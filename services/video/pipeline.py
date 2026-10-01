"""
Полный цикл сборки видеообложки WB по артикулу: фото с карточки →
Vision-отбор чистых кадров (см. память bot-v2-services-ozon-wb про
рекламные баннеры/коробки, спрятанные в фотосете) → R2 → Hailuo.
Используется и из handlers/video.py (команда /video), и из разовых
тестовых скриптов — чтобы не дублировать логику.
"""
import asyncio
import io
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path

import numpy as np

from services.wb_content import get_wb_card_data, upload_video as wb_upload_video
from services.storage.r2 import upload_image, upload_video
from services.image.gemini import classify_video_frame, describe_shape_for_video
from services.image.clip_scorer import get_image_embedding
from services.video.hailuo import generate_video
from utils.gpu_safety import run_gpu

log = logging.getLogger(__name__)

# 29.07.2026 (запрос пользователя — «просмотреть ВСЕ фото, чтоб 3D-прокрутка
# со всех сторон»): раньше резали до 8 фото ДО отбора, теперь смотрим
# практически весь фотосет карточки — WB редко даёт больше ~20 фото, а Vision-
# классификация одного кадра копеечная (см. classify_video_frame).
_MAX_CHECK = 20

# 29.07.2026: сначала пробовали САМУЮ дешёвую схему — 512P, без конечного
# опорного кадра. РЕЗУЛЬТАТ: без конечного кадра Hailuo на orbit-повороте
# додумывает форму товара (живой баг — деформировалась мышь на тесте,
# "вообще полностью выдумка нейронки"). Конечный кадр — главный рычаг
# контроля формы (см. память project_wb_video_cover), без него нельзя.
# Поэтому вернули: одиночный клип снова с конечным опорным кадром → Hailuo
# сам бампает до 768P (first-last-frame не работает на 512P, см.
# services/video/hailuo.py), duration=10с. CHEAP_MODE теперь отвечает
# ТОЛЬКО за то, что не включаем дорогую двухклиповую 16с-версию
# (build_wb_video_long) — она на порядок дороже одиночного клипа.
CHEAP_MODE = True

# Кэш платных артефактов Hailuo: клипы + инфографика на диске, чтобы
# пересборка (переходы/склейка/интро) была БЕСПЛАТНОЙ — не гонять
# генерацию заново из-за косяка на этапе монтажа (запрос пользователя
# 29.07.2026: инструментарий против «косячных видео» без лишних трат).
_CACHE_ROOT = Path("logs") / "video_cache"


def _cache_dir(article: str) -> Path:
    d = _CACHE_ROOT / article
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_save(article: str, title: str, clips: list[bytes], intro_image: bytes | None):
    try:
        d = _cache_dir(article)
        (d / "title.txt").write_text(title, encoding="utf-8")
        for i, clip in enumerate(clips, 1):
            (d / f"clip{i}.mp4").write_bytes(clip)
        if intro_image:
            (d / "intro.jpg").write_bytes(intro_image)
    except Exception as e:
        log.warning(f"video cache save {article}: {e}")  # кэш вспомогательный, не роняем генерацию


def _cache_load(article: str) -> tuple[str, list[bytes], bytes | None] | None:
    d = _CACHE_ROOT / article
    if not (d / "clip1.mp4").exists():
        return None
    title = (d / "title.txt").read_text(encoding="utf-8") if (d / "title.txt").exists() else article
    clips = []
    for i in (1, 2):
        p = d / f"clip{i}.mp4"
        if p.exists():
            clips.append(p.read_bytes())
    intro = (d / "intro.jpg").read_bytes() if (d / "intro.jpg").exists() else None
    return title, clips, intro

_COMMON_FRAMING = (
    "Premium e-commerce product hero shot for a marketplace listing. "
    "The product itself stays perfectly static, sharp and undistorted — its exact "
    "shape, proportions, logo and any text must not warp or change. The product "
    "remains a single solid, fully assembled object at all times — nothing opens, "
    "splits, separates, or floats apart. Absolutely NO exploded-view diagram, NO "
    "cutaway or X-ray view, NO labeled parts callouts, NO disassembly or teardown "
    "of any kind — this is a plain orbiting camera shot of the closed product, not "
    "a technical breakdown. Only the camera moves, in a smooth, slow, cinematic "
    "studio motion. Clean seamless studio background (light gray or dark gradient), "
    "no camera shake, no extra objects, no people, no watermark, no text overlays. "
    "Polished, high-end retail advertising look — the kind of hero shot used for a "
    "flagship product listing."
)


def build_prompt(title: str, part: int = 0, shape_note: str = "") -> str:
    """Промпт для карточки WB — задача не «красивое видео вообще», а
    товарная презентация: продукт статичен и узнаваем (форма/шрифт на
    шильдиках не должны плыть), двигается только камера, а «энергия»
    навешивается на свет/блики, а не на скорость движения (см. память
    project_wb_video_cover — так меньше риск исказить геометрию).
    part=0 — единственный клип (обычный /video). part=1/2 — два клипа
    длинной версии (10с+6с), движение камеры продолжается в ту же
    сторону между ними, чтобы стык не дёргался.
    С 29.07.2026 ФИНАЛ клипа камера замедляет и «устаканивает» на
    стабильном центрированном ракурсе — видео заканчивается на спокойном
    кадре, а не обрывает движение на полуслове (раньше Hailuo резал
    орбиту на произвольном кадре).
    shape_note — короткое Vision-описание реальной геометрии товара
    (см. describe_shape_for_video), явно ограничивает orbit-движение для
    плоских панельных товаров (антенны/точки доступа), которые Hailuo
    раздувает/растягивает при повороте, фабрикуя несуществующий объём
    (найдено на CPE210 13.07.2026)."""
    if part == 1:
        movement = (
            "[push in] [slow orbit right] Elegant opening: the camera starts at a "
            "neutral distance and smoothly pushes in toward the product while rim "
            "lighting gradually blooms across its surface — a premium reveal. The "
            "camera then begins a slow orbit around the product, establishing its "
            "overall shape."
        )
    elif part == 2:
        movement = (
            "[continue orbit right] The camera continues orbiting in the same "
            "direction as before, then gradually decelerates and comes to a gentle, "
            "complete stop on a centered, stable hero angle with the full product in "
            "frame. In the final second the camera is nearly still — a calm, settled "
            "closing frame. Lighting eases into an even, polished premium glow."
        )
    else:
        movement = (
            "[push in] [slow orbit right] Elegant opening: the camera smoothly pushes "
            "in as soft rim lighting blooms across the surface, then slowly orbits the "
            "product. Toward the end the camera gradually decelerates and settles to a "
            "gentle stop on a centered, stable hero angle — the final second is nearly "
            "still, a calm closing frame."
        )
    shape_constraint = (
        f" Real physical shape of the product: {shape_note}. Respect this exact "
        f"geometry — do not invent volume, thickness or depth that isn't there; "
        f"if it's a thin flat panel, keep it visually flat throughout the motion."
        if shape_note else ""
    )
    return f"{title}. {_COMMON_FRAMING} {movement}{shape_constraint}"


def _product_frame_coverage(photo: bytes) -> float:
    """Грубая CPU-оценка (без rembg/GPU — сознательно, см. память про
    перегрузку системы 12.07.2026), какую долю кадра занимает товар: bbox
    непохожих-на-фон пикселей / площадь кадра. Нужна, чтобы для видео
    выбирать крупные close-up кадры, а не мелкие lifestyle-снимки, где товар
    теряется в кадре (запрос пользователя 13.07.2026 — «где крупно есть
    товар»). WB-студийные фото почти всегда на светлом/белом фоне — фон
    оцениваем по углам кадра."""
    from PIL import Image
    img = Image.open(io.BytesIO(photo)).convert("RGB").resize((200, 200))
    arr = np.asarray(img).astype(int)
    sz = 15
    corners = np.concatenate([
        arr[:sz, :sz].reshape(-1, 3), arr[:sz, -sz:].reshape(-1, 3),
        arr[-sz:, :sz].reshape(-1, 3), arr[-sz:, -sz:].reshape(-1, 3),
    ])
    bg_color = corners.mean(axis=0)
    diff = np.abs(arr - bg_color).sum(axis=2)
    mask = diff > 40
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return 0.0
    bbox_area = (rows[-1] - rows[0] + 1) * (cols[-1] - cols[0] + 1)
    return bbox_area / (200 * 200)


async def _classify_photos(photos: list[bytes], title: str) -> tuple[list[tuple[bytes, float]], bytes | None]:
    """ОДИН проход по фото карточки через единый видео-классификатор (см.
    classify_video_frame — лояльный к фону/контексту, не строгие правила
    карточки маркетплейса). Раньше строгая validate_product_image отсеивала
    ВСЕ фото у сетевого оборудования (антенны/точки доступа — обычно кадры
    установки на объекте, не студийные), и в дело для вращения шло что
    попало (баг найден 13.07.2026 на NanoBeam/CPE610). Теперь: PRODUCT —
    можно вращать, PROMO — годится только как интро, BAD/UNKNOWN — не
    используется нигде. Возвращает СЫРОЙ scored-список (photo, coverage) —
    сортировку/отбор ракурсов делает _select_angles (см. ниже, 29.07.2026:
    просто «самые крупные» ≠ «разные стороны товара»)."""
    scored: list[tuple[bytes, float]] = []
    promo_photo: bytes | None = None
    for photo in photos[:_MAX_CHECK]:
        try:
            verdict = await classify_video_frame(photo, title)
        except Exception as e:
            log.warning(f"video classify ошибка, пропуск фото: {e}")
            continue
        if verdict == "PRODUCT":
            scored.append((photo, _product_frame_coverage(photo)))
        elif verdict == "PROMO" and promo_photo is None:
            promo_photo = photo
    scored.sort(key=lambda c: c[1], reverse=True)
    log.info(f"video classify: {len(scored)} товарных кадров из "
             f"{len(photos[:_MAX_CHECK])} проверенных, промо-графика "
             f"{'найдена' if promo_photo else 'не найдена'}")
    return scored, promo_photo


async def _select_angles(scored: list[tuple[bytes, float]], n: int) -> list[bytes]:
    """Отбирает n визуально РАЗНЫХ ракурсов товара — не просто n самых
    крупных кадров (запрос пользователя 29.07.2026: «просмотреть все фото,
    чтоб 3D-прокрутка со всех сторон»). Метод: CLIP-эмбеддинг (локально на
    GPU, бесплатно — см. get_image_embedding) для каждого кандидата, потом
    greedy farthest-point selection — старт с самого крупного кадра
    (обычно лучший фронтальный план), дальше на каждом шаге добавляем
    кадр с максимальным МИНИМАЛЬНЫМ косинусным расстоянием до уже
    выбранных. Это и даёт «со всех сторон», а не N похожих крупных планов
    одного ракурса. При сбое GPU/эмбеддинга — фолбэк на старое поведение
    (топ-n по размеру в кадре)."""
    if len(scored) <= n:
        return [p for p, _ in scored]

    photos = [p for p, _ in scored]
    embeddings = []
    for p in photos:
        emb = await run_gpu(get_image_embedding, p, timeout=15, default=None,
                             label="video frame embedding")
        embeddings.append(emb)
    if any(e is None for e in embeddings):
        log.warning("video: CLIP-эмбеддинг недоступен, фолбэк на отбор по размеру кадра")
        return photos[:n]

    selected = [0]
    while len(selected) < n:
        best_idx, best_dist = None, -1.0
        for i in range(len(photos)):
            if i in selected:
                continue
            dist = min(1.0 - float(np.dot(embeddings[i], embeddings[j])) for j in selected)
            if dist > best_dist:
                best_dist, best_idx = dist, i
        selected.append(best_idx)
    return [photos[i] for i in selected]


async def _pick_start_end(scored: list[tuple[bytes, float]], raw_photos: list[bytes]) -> tuple[bytes, bytes | None]:
    """start/end для одного клипа — оба из товарных кадров, максимально
    РАЗНЫЕ ракурсы (см. _select_angles). Фолбэк на сырые фото ТОЛЬКО если
    вообще ни одного товарного кадра не нашлось (риск, но лучше чем совсем
    не сделать видео) — не блокируем поток."""
    if not scored:
        log.warning("video: ни одного пригодного товарного кадра — беру первое сырое фото (риск)")
        return raw_photos[0], (raw_photos[1] if len(raw_photos) > 1 else None)
    diverse = await _select_angles(scored, 2)
    return diverse[0], (diverse[1] if len(diverse) > 1 else None)


async def _pick_triplet(scored: list[tuple[bytes, float]], raw_photos: list[bytes]) -> tuple[bytes, bytes | None, bytes | None]:
    """Как _pick_start_end, но до 3 максимально разных ракурсов — под
    двухклиповую длинную версию."""
    if not scored:
        log.warning("video: ни одного пригодного товарного кадра — беру сырые (риск)")
        raw = raw_photos[:3]
        return raw[0], (raw[1] if len(raw) > 1 else None), (raw[2] if len(raw) > 2 else None)
    diverse = (await _select_angles(scored, 3) + [None, None])[:3]
    return diverse[0], diverse[1], diverse[2]


async def build_wb_video(article: str) -> tuple[bytes, str]:
    """Полный цикл по артикулу: WB-фото → Vision-отбор → R2 → Hailuo →
    интро с инфографикой (3с) → ПЛАВНЫЙ кроссфейд → прокрутка товара.
    Структура подтверждена пользователем 29.07.2026: инфографика ТОЛЬКО в
    начале, конец — на клипе Hailuo (камера сама замедляется и
    останавливается, см. build_prompt — обрыва движения нет). Платные
    артефакты кэшируются на диск (_cache_save) — пересборку монтажа можно
    делать бесплатно (rebuild_video_from_cache).
    Возвращает (video_bytes, title). Бросает ValueError если карточки/фото нет."""
    card = await get_wb_card_data(article)
    if not card or not card["photos"]:
        raise ValueError(f"карточка {article} не найдена на WB или без фото")

    title = card["title"] or article
    scored, promo_photo = await _classify_photos(card["photos"], title)
    start_photo, end_photo = await _pick_start_end(scored, card["photos"])

    start_url = await upload_image(start_photo)
    if not start_url:
        raise RuntimeError("не удалось залить стартовое фото на R2")
    end_url = await upload_image(end_photo) if end_photo else None

    shape_note = await describe_shape_for_video(start_photo, title)
    prompt = build_prompt(title, shape_note=shape_note)
    # duration=10 (макс. для одного клипа Hailuo standard) — 6с показался
    # пользователю коротким (13.07.2026). Конечный опорный кадр ОБЯЗАТЕЛЕН
    # (см. комментарий у CHEAP_MODE выше — без него Hailuo деформирует
    # форму товара на orbit-повороте) — Hailuo сам бампает разрешение до
    # 768P, когда end_url задан.
    clip = await generate_video(prompt, start_url, end_url, duration="10")
    intro_image = promo_photo or await _build_intro_infographic(card, start_photo)
    _cache_save(article, title, [clip], intro_image)
    return await _prepend_intro(clip, intro_image), title


def _ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _ffprobe_video_specs(path: Path) -> tuple[int, int, float]:
    """width, height, fps реального клипа — интро рендерим с теми же
    параметрами, иначе склейка требует пересжатия/скейлинга и может
    дать артефакты на стыке."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h, fr = out.split(",")
    num, _, den = fr.partition("/")
    fps = float(num) / float(den) if den and float(den) else float(num)
    return int(w), int(h), fps


def _frames_to_mp4(frames: list, width: int, height: int, fps: float) -> bytes:
    with tempfile.TemporaryDirectory(prefix="wb_video_intro_") as tmp:
        out = Path(tmp) / "intro.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{fps:.3f}",
            "-i", "-",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            str(out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for fr in frames:
            proc.stdin.write(fr.tobytes())
        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg intro encode failed (code {proc.returncode})")
        return out.read_bytes()


def _cover_crop_native(img, width: int, height: int):
    """Обрезка по центру под нужные пропорции БЕЗ ресайза, на полном
    разрешении исходника — оставляет запас пикселей, чтобы дальше было
    куда «зумиться» окном кропа (см. _render_intro)."""
    src_ratio = img.width / img.height
    dst_ratio = width / height
    if src_ratio > dst_ratio:
        new_w = round(img.height * dst_ratio)
        left = (img.width - new_w) // 2
        return img.crop((left, 0, left + new_w, img.height))
    new_h = round(img.width / dst_ratio)
    top = (img.height - new_h) // 2
    return img.crop((0, top, img.width, top + new_h))


def _render_intro(image: bytes, width: int, height: int, fps: float, duration: float = 3.0) -> bytes:
    """Лёгкий наезд камеры (Ken Burns) на готовую инфографику — не мёртвая
    статика. duration=3с по прямому запросу пользователя 13.07.2026.
    «Уход» инфографики — плавный кроссфейд в основной клип
    (_prepend_intro), не резкий обрыв и не покадровая анимация текста.
    Чистый CPU/PIL+ffmpeg.

    РАНЬШЕ на каждом кадре исходник ресайзился заново в округлённый
    (round(width*zoom)) промежуточный размер, а потом обрезался — двойное
    округление на кадр (внешнее + внутри старого _cover_resize) давало
    чуть разный паттерн LANCZOS-передискретизации кадр к кадру → заметная
    дрожь мелких деталей/текста при увеличении (жалоба пользователя
    13.07.2026). Теперь: один раз кропим оригинал под пропорции на полном
    разрешении (_cover_crop_native), а дальше на каждом кадре меняется
    только ОКНО кропа (сжимается к центру по мере зума), и это окно всегда
    ресайзится ОДНИМ вызовом в фиксированный width×height — стабильный
    паттерн передискретизации, дрожи нет."""
    from PIL import Image

    src = Image.open(io.BytesIO(image)).convert("RGB")
    base = _cover_crop_native(src, width, height)
    bw, bh = base.size
    n_frames = max(1, round(duration * fps))
    ZOOM_END = 1.06

    frames = []
    for i in range(n_frames):
        zoom = 1.0 + (ZOOM_END - 1.0) * (i / max(1, n_frames - 1))
        crop_w = bw / zoom
        crop_h = bh / zoom
        left = (bw - crop_w) / 2
        top = (bh - crop_h) / 2
        box = (round(left), round(top), round(left + crop_w), round(top + crop_h))
        frames.append(base.crop(box).resize((width, height), Image.LANCZOS))

    return _frames_to_mp4(frames, width, height, fps)


async def _build_intro_infographic(card: dict, product_photo: bytes) -> bytes | None:
    """Готовая инфографика бота (стиль simple — та же система, что делает
    карточки WB/Ozon), а не самодельные плашки: пользователь 13.07.2026
    забраковал бейджи поверх видео и попросил вернуть инфографику, но с
    плавным переходом. Единственное место в видео-пайплайне с LLM
    (фичи/слоган) и rembg (вырезка товара, через run_gpu — см. gpu_safety)
    — один раз на видео, не батчем. None при любой проблеме — тогда видео
    идёт без интро, сразу с основного клипа."""
    try:
        from services.llm import get_llm
        from services.card.normalize import parse_product_info
        from services.card.category import detect_category
        from services.image import make_simple_infographic
        from handlers.image import _extract_visuals
        from utils.billing import save_cost
        from config import settings

        llm = await get_llm(settings.admin_id)
        title = card.get("title") or ""
        info = await parse_product_info(title, llm)
        product_name = info.full_name or title
        category = detect_category(product_name) or ""
        context = "\n".join(f"{n}: {v}" for n, v in (card.get("characteristics") or []))

        features, slogan, tips, rich_slogan, resps = await _extract_visuals(
            context, product_name, category, llm,
        )
        for resp in resps:
            await save_cost(settings.admin_id, "video_intro", response=resp)

        infographic, warning = await make_simple_infographic(
            product_name, features, tips, product_photo, llm=llm,
            brand=info.brand, category=category,
        )
        if warning:
            log.warning(f"video intro infographic: {warning}")
        return infographic
    except Exception as e:
        log.warning(f"video intro infographic failed, видео пойдёт без интро: {e}")
        return None


async def _prepend_intro(video_bytes: bytes, intro_image: bytes | None) -> bytes:
    """Интро (3с, лёгкий наезд камеры на инфографику) → ПЛАВНЫЙ кроссфейд
    (1.2с — длиннее обычного, чтобы переход не выглядел резким обрывом,
    как забраковал пользователь 13.07.2026) → основной Hailuo-клип (чистый
    товар без бейджей, начинает вращаться). Инфографика только в начале —
    подтверждено пользователем 29.07.2026."""
    if intro_image is None:
        return video_bytes

    def _render_and_stitch() -> bytes:
        with tempfile.TemporaryDirectory(prefix="wb_video_main_") as tmp:
            main_path = Path(tmp) / "main.mp4"
            main_path.write_bytes(video_bytes)
            width, height, fps = _ffprobe_video_specs(main_path)
        intro_bytes = _render_intro(intro_image, width, height, fps)
        return _stitch_xfade(intro_bytes, video_bytes, crossfade=1.2)

    return await asyncio.to_thread(_render_and_stitch)


def _stitch_xfade(clip1: bytes, clip2: bytes, crossfade: float = 0.5) -> bytes:
    """Склеивает два клипа кроссфейдом (чистый CPU, libx264 — GPU занят
    ботом, см. gpu_safety/video_proto.py). offset считается по РЕАЛЬНОЙ
    длительности клипа 1 (ffprobe) — Hailuo не всегда отдаёт ровно
    запрошенные секунды."""
    with tempfile.TemporaryDirectory(prefix="wb_video_") as tmp:
        tmp = Path(tmp)
        p1, p2, out = tmp / "clip1.mp4", tmp / "clip2.mp4", tmp / "out.mp4"
        p1.write_bytes(clip1)
        p2.write_bytes(clip2)
        dur1 = _ffprobe_duration(p1)
        offset = max(dur1 - crossfade, 0.1)
        cmd = [
            "ffmpeg", "-y", "-i", str(p1), "-i", str(p2),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={crossfade}:offset={offset:.3f}[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            str(out),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        return out.read_bytes()


async def build_wb_video_long(article: str) -> tuple[bytes, str]:
    """Длинная версия (~15-16с, максимум — дальше Hailuo не тянет вменяемо):
    два клипа Hailuo (10с + 6с, оба 768P — first-last-frame поддерживает
    только 768P, см. память/находку 13.07.2026) со сквозным кадром на стыке
    (конец клипа 1 = начало клипа 2), склеенные кроссфейдом 0.5с.
    Фолбэк на обычный build_wb_video (один клип 10с), если у карточки МЕНЬШЕ
    3 разных товарных кадров — 16с ради 16с не делаем: если показывать
    нечего (2 похожих ракурса), длинный ровный проезд ощущается затянутым и
    пустым (докстанция, фидбэк пользователя 13.07.2026). Только когда
    контента реально хватает на 3 разных ракурса — используем полные 16с.
    При CHEAP_MODE=True (см. флаг выше) сразу отдаём дешёвую build_wb_video —
    двухклиповая 768P-версия на порядок дороже, пока сидим на дешёвой."""
    if CHEAP_MODE:
        return await build_wb_video(article)
    card = await get_wb_card_data(article)
    if not card or not card["photos"]:
        raise ValueError(f"карточка {article} не найдена на WB или без фото")

    title = card["title"] or article
    scored, promo_photo = await _classify_photos(card["photos"], title)
    frame1, frame2, frame3 = await _pick_triplet(scored, card["photos"])
    if frame2 is None or frame3 is None:
        log.info(f"video build_wb_video_long {article}: меньше 3 разных товарных кадров, "
                 f"делаю один клип (10с) вместо растянутых 16с без контента")
        return await build_wb_video(article)

    url1 = await upload_image(frame1)
    url2 = await upload_image(frame2)
    if not url1 or not url2:
        raise RuntimeError("не удалось залить опорные кадры на R2")
    url3 = await upload_image(frame3) if frame3 else None

    shape_note = await describe_shape_for_video(frame1, title)
    clip1 = await generate_video(build_prompt(title, part=1, shape_note=shape_note), url1, url2, resolution="768P", duration="10")
    clip2 = await generate_video(build_prompt(title, part=2, shape_note=shape_note), url2, url3, resolution="768P", duration="6")

    combined = await asyncio.to_thread(_stitch_xfade, clip1, clip2)
    intro_image = promo_photo or await _build_intro_infographic(card, frame1)
    _cache_save(article, title, [clip1, clip2], intro_image)
    return await _prepend_intro(combined, intro_image), title


async def preview_video_frames(article: str) -> tuple[str, list[bytes], bytes | None, str]:
    """БЕСПЛАТНЫЙ предпросмотр перед генерацией (только копеечный Vision):
    какие кадры пойдут в Hailuo как опорные, какая промо-графика найдена,
    и какой промпт будет отправлен. Позволяет отловить косячный отбор
    кадров ДО того, как потрачены деньги на генерацию (запрос пользователя
    29.07.2026). Показывает РЕАЛЬНУЮ схему текущего режима — при CHEAP_MODE
    это 2 кадра (старт+финал, обязательный конечный опорный кадр — без него
    Hailuo деформирует форму товара) и промпт part=0 (как в build_wb_video),
    иначе триплет с part=1 (как в build_wb_video_long). Возвращает (title,
    опорные кадры по порядку, промо-кадр или None, текст промпта)."""
    card = await get_wb_card_data(article)
    if not card or not card["photos"]:
        raise ValueError(f"карточка {article} не найдена на WB или без фото")

    title = card["title"] or article
    scored, promo_photo = await _classify_photos(card["photos"], title)
    if CHEAP_MODE:
        frame1, frame2 = await _pick_start_end(scored, card["photos"])
        frames = [f for f in (frame1, frame2) if f is not None]
        shape_note = await describe_shape_for_video(frame1, title)
        prompt = build_prompt(title, shape_note=shape_note)
    else:
        frame1, frame2, frame3 = await _pick_triplet(scored, card["photos"])
        frames = [f for f in (frame1, frame2, frame3) if f is not None]
        shape_note = await describe_shape_for_video(frame1, title)
        prompt = build_prompt(title, part=1, shape_note=shape_note)
    return title, frames, promo_photo, prompt


async def build_draft_video(article: str) -> tuple[bytes, str]:
    """ДЕШЁВЫЙ черновик для проверки движения/промпта: один клип 512P/6с
    без опорного конечного кадра (512P работает только так) и без монтажа —
    сырой выход Hailuo. На порядок дешевле полной генерации (512P в 8 раз
    дешевле 768P, логика движения та же — см. память project_wb_video_cover).
    Если движение устраивает — гонять полную версию."""
    card = await get_wb_card_data(article)
    if not card or not card["photos"]:
        raise ValueError(f"карточка {article} не найдена на WB или без фото")

    title = card["title"] or article
    scored, _ = await _classify_photos(card["photos"], title)
    start_photo, _ = await _pick_start_end(scored, card["photos"])
    start_url = await upload_image(start_photo)
    if not start_url:
        raise RuntimeError("не удалось залить стартовое фото на R2")

    shape_note = await describe_shape_for_video(start_photo, title)
    prompt = build_prompt(title, shape_note=shape_note)
    clip = await generate_video(prompt, start_url, None, resolution="512P", duration="6")
    return clip, title


async def rebuild_video_from_cache(article: str) -> tuple[bytes, str]:
    """БЕСПЛАТНАЯ пересборка из кэша последней генерации (_cache_save):
    интро с инфографикой + склейка клипов — чистый CPU/ffmpeg, ни одного
    платного вызова. Нужна, когда сами клипы Hailuo хорошие, а косяк в
    монтаже (переход, длительность интро и т.п.) — правим код монтажа и
    пересобираем без повторной оплаты генерации."""
    cached = _cache_load(article)
    if cached is None:
        raise ValueError(f"кэша для {article} нет — сначала полная генерация /video {article}")
    title, clips, intro_image = cached

    combined = clips[0]
    if len(clips) > 1:
        combined = await asyncio.to_thread(_stitch_xfade, clips[0], clips[1])
    combined = await _prepend_intro(combined, intro_image)
    return combined, title


_OZON_VIDEO_COVER_ATTR_ID = 21845     # «Озон.Видеообложка: ссылка»
_OZON_VIDEO_COVER_COMPLEX_ID = 100002  # 8-30 сек, MP4/MOV, до 20МБ

_OZON_VIDEO_LINK_ATTR_ID = 21841   # «Озон.Видео: ссылка» — обычное видео в галерее (не обложка)
_OZON_VIDEO_TITLE_ATTR_ID = 21837  # «Озон.Видео: название» — обязательная пара к ссылке
_OZON_VIDEO_COMPLEX_ID = 100001    # 8с-5мин, MP4/MOV, до 2ГБ (сверено живьём по /v1/description-category/attribute 13.07.2026)


async def upload_video_to_wb(article: str, video_bytes: bytes) -> bool:
    """Загружает видео прямо на карточку WB — POST /content/v3/media/file
    (multipart, поле uploadfile, макс. 1 видео на карточку, ≤50Мб, MP4/MOV,
    см. офиц. OpenAPI-спеку wildberries-sdk, сверено 13.07.2026). Раньше
    ошибочно считали, что видео нельзя загрузить через API вообще (спутали
    с отдельной страницей про AI-генерацию «Джем» в кабинете) — это не так,
    обычная ручная загрузка видео тоже доступна программно."""
    card = await get_wb_card_data(article)
    if not card or not card.get("nmID"):
        log.info(f"video WB: карточка {article!r} не найдена — пропуск загрузки видео")
        return False
    resp = await wb_upload_video(card["nmID"], video_bytes)
    if resp["status"] != 200 or resp["body"].get("error"):
        log.warning(f"video WB: загрузка видео для {article!r} не удалась: {resp}")
        return False
    log.info(f"video WB: видео загружено на карточку {article!r} (nmID={card['nmID']})")
    return True


async def upload_video_to_ozon(offer_id: str, video_bytes: bytes) -> bool:
    """Если товар с этим offer_id есть на Ozon — заливает готовое видео на
    R2 и проставляет ОБА видео-атрибута: «Видеообложка» (id 21845, короткий
    превью-ролик, 8-30с) И обычное «Видео» в галерее товара (id 21841
    ссылка + id 21837 название, комплекс 100001, 8с-5мин) — по запросу
    пользователя 13.07.2026, чтобы видео было видно не только обложкой,
    но и полноценно в галерее рядом с фото.
    False, если товара с таким offer_id на Ozon нет, или обложка/видео не
    удалось проставить — не блокируем основной поток (видео в Telegram
    всё равно уходит)."""
    from services.ozon.client import get_products_attributes, update_product_attributes

    try:
        existing = await get_products_attributes([offer_id])
    except Exception as e:
        log.warning(f"video Ozon: не удалось проверить наличие offer_id={offer_id!r}: {e}")
        return False
    if offer_id not in existing:
        log.info(f"video Ozon: offer_id={offer_id!r} не найден на Ozon — пропуск")
        return False

    url = await upload_video(video_bytes)
    if not url:
        log.warning(f"video Ozon: не удалось залить видео на R2 для {offer_id!r}")
        return False

    title = (existing[offer_id].get("name") or offer_id)[:150]
    ok = True

    try:
        await update_product_attributes(offer_id, [{
            "id": _OZON_VIDEO_COVER_ATTR_ID,
            "complex_id": _OZON_VIDEO_COVER_COMPLEX_ID,
            "values": [{"value": url}],
        }])
        log.info(f"video Ozon: видеообложка проставлена для {offer_id!r} -> {url}")
    except Exception as e:
        log.warning(f"video Ozon: не удалось проставить видеообложку для {offer_id!r}: {e}")
        ok = False

    try:
        await update_product_attributes(offer_id, [
            {"id": _OZON_VIDEO_LINK_ATTR_ID, "complex_id": _OZON_VIDEO_COMPLEX_ID, "values": [{"value": url}]},
            {"id": _OZON_VIDEO_TITLE_ATTR_ID, "complex_id": _OZON_VIDEO_COMPLEX_ID, "values": [{"value": title}]},
        ])
        log.info(f"video Ozon: видео в галерее проставлено для {offer_id!r} -> {url}")
    except Exception as e:
        log.warning(f"video Ozon: не удалось проставить видео в галерее для {offer_id!r}: {e}")
        ok = False

    return ok
