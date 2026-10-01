import io
import logging
import os
import threading
from PIL import Image

log = logging.getLogger(__name__)

# 20.07.2026: та же гонка, что чинили в clip_scorer.py — _GPU_EXECUTOR
# (utils/gpu_safety.py) допускает 2 параллельных потока, и без лока оба
# могли одновременно увидеть session=None и загрузить birefnet-general
# ДВАЖДЫ (поймано живьём: "rembg session initialized (birefnet-general)"
# два раза подряд в одном прогоне) — двойная модель в VRAM, лишний вклад
# в сегодняшнюю деградацию GPU. Один лок на все 4 сессии — они грузятся
# редко (один раз за процесс), контеншен не проблема.
_session_lock = threading.Lock()


def _no_arena_opts():
    """21.07.2026: onnxruntime CPU-арена по умолчанию (enable_cpu_mem_arena=True)
    растёт с каждым новым размером входа и НЕ отдаёт память обратно ОС —
    подтверждено замером: 4 вызова remove() подряд без этой опции добавляли
    гигабайты RSS без возврата даже после del+gc.collect(), с опцией — RSS
    стабилен (+6МБ на первом вызове, 0 на следующих). Это и было настоящей
    причиной обвалов RAM на "трудных" карточках с богатой фото-выдачей (см.
    память ai_bot_v2_migration) — не размер картинки и не конкурентность,
    а сама арена. Общая опция для всех 4 сессий rembg ниже."""
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.enable_cpu_mem_arena = False
    return opts


# 17.09.2026: Моноблоки — тот же плоский экран на подставке, что и мониторы
# (живой баг DQ.BPGEC.004: generic remove_background оставил белый контур
# по кромке панели и белый блоб у подставки — remove_background_monitor
# уже решает ровно эту проблему для category == "Мониторы", но моноблоки
# были не покрыты). Общий список для всех вызывающих мест.
MONITOR_LIKE_CATEGORIES = {"Мониторы", "Моноблоки"}

_session = None
_yolo_model = None
_YOLO_MODEL_PATH = os.path.join(os.path.dirname(__file__), "../../data/yolov8n.pt")
_YOLO_PHONE_CLASS = 67  # COCO: cell phone


def _get_yolo():
    global _yolo_model
    if _yolo_model is None:
        try:
            from ultralytics import YOLO
            _yolo_model = YOLO(_YOLO_MODEL_PATH)
            log.info("YOLOv8n loaded")
        except Exception as e:
            log.warning(f"YOLO load failed: {e}")
    return _yolo_model


def _yolo_crop_phone(img: Image.Image) -> Image.Image | None:
    """Детектирует телефоны через YOLOv8, возвращает кроп наибольшего.
    Если телефонов не найдено — None."""
    model = _get_yolo()
    if model is None:
        return None
    try:
        results = model(img, verbose=False, classes=[_YOLO_PHONE_CLASS])
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return None
        # Берём бокс с наибольшей площадью
        best = max(boxes, key=lambda b: float((b.xyxy[0][2] - b.xyxy[0][0]) *
                                              (b.xyxy[0][3] - b.xyxy[0][1])))
        x0, y0, x1, y1 = (int(v) for v in best.xyxy[0].tolist())
        # Буфер 5% от размера bbox — не срезаем края телефона
        bw, bh = x1 - x0, y1 - y0
        buf = max(20, int(min(bw, bh) * 0.05))
        x0, y0 = max(0, x0 - buf), max(0, y0 - buf)
        x1, y1 = min(img.width, x1 + buf), min(img.height, y1 + buf)
        cropped = img.crop((x0, y0, x1, y1))
        conf = float(best.conf[0])
        log.info(f"YOLO: {len(boxes)} телефонов, кроп наибольшего {cropped.size} conf={conf:.2f}")
        if conf < 0.60:
            log.info(f"YOLO conf={conf:.2f} < 0.60 — ложная цель (кабель/коннектор?), пропускаем")
            return None
        return cropped
    except Exception as e:
        log.warning(f"YOLO detect failed: {e}")
        return None


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                from rembg import new_session
                _session = new_session("birefnet-general", sess_opts=_no_arena_opts())
                log.info("rembg session initialized (birefnet-general)")
    return _session


_session_cpu = None

def _get_session_cpu():
    """CPU-сессия birefnet — фолбэк, когда CUDA не может выделить память
    (видеокарта общая с рабочим столом: Chrome/Steam/игры съедают VRAM,
    см. 16.07 — постоянные ONNXRuntimeError на почти пустой GPU)."""
    global _session_cpu
    if _session_cpu is None:
        with _session_lock:
            if _session_cpu is None:
                from rembg import new_session
                _session_cpu = new_session("birefnet-general", providers=["CPUExecutionProvider"],
                                            sess_opts=_no_arena_opts())
                log.info("rembg session initialized (birefnet-general, CPU fallback)")
    return _session_cpu


_session_u2net = None

def _get_session_u2net():
    global _session_u2net
    if _session_u2net is None:
        with _session_lock:
            if _session_u2net is None:
                from rembg import new_session
                _session_u2net = new_session("u2net", sess_opts=_no_arena_opts())
                log.info("rembg session initialized (u2net)")
    return _session_u2net


_session_isnet = None

def _get_session_isnet():
    global _session_isnet
    if _session_isnet is None:
        with _session_lock:
            if _session_isnet is None:
                from rembg import new_session
                _session_isnet = new_session("isnet-general-use", sess_opts=_no_arena_opts())
                log.info("rembg session initialized (isnet-general-use)")
    return _session_isnet


def _already_transparent(img_bytes: bytes) -> Image.Image | None:
    """Если файл уже PNG с прозрачным фоном — возвращает RGBA-image
    (без вызова rembg). Иначе None.
    Проверяем по углам: если все 4 угла непрозрачны — фон не убран
    (WB часто шлёт PNG с антиалиасинговыми краями, но белым фоном)."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode != "RGBA" and "transparency" not in img.info:
            return None
        img = img.convert("RGBA")
        import numpy as np
        alpha_arr = np.array(img.getchannel("A"))
        total = alpha_arr.size
        if total == 0:
            return None
        transparent = int((alpha_arr < 30).sum())
        ratio = transparent / total
        if ratio < 0.005:
            return None  # почти нет прозрачных → фон не убран
        if ratio < 0.40:
            # Мало прозрачных пикселей (5–40%) — WB-PNG с прозрачным
            # канвасом, но белым прямоугольником вокруг продукта внутри.
            # Настоящий прозрачный фон даёт 60–80% прозрачности.
            log.info(f"PNG: прозрачность {ratio:.1%} < 40% — запускаем rembg (белый фон внутри)")
            return None
        # Проверка углов ВСЕГО изображения: непрозрачные углы = фон не убран
        # (любого цвета — раньше проверялась только непрозрачность как
        # таковая, это ок, здесь баг не был белый-специфичным).
        tl = int(alpha_arr[0, 0])
        tr = int(alpha_arr[0, -1])
        bl = int(alpha_arr[-1, 0])
        br = int(alpha_arr[-1, -1])
        if tl >= 200 and tr >= 200 and bl >= 200 and br >= 200:
            log.info(f"PNG: углы непрозрачны (tl={tl} tr={tr} bl={bl} br={br}) — запускаем rembg")
            return None

        # Дополнительная проверка: WB часто шлёт PNG с прозрачным канвасом
        # снаружи, но белым прямоугольником ВНУТРИ (фон не вырезан).
        # Если углы BOUNDING BOX bbox — белые и непрозрачные → rembg.
        try:
            from PIL import Image as _pil
            bbox = _pil.fromarray(alpha_arr).getbbox()
            if bbox:
                bx0, by0, bx1, by1 = bbox
                bh, bw = by1 - by0, bx1 - bx0
                sz = max(5, min(bh, bw) // 12)
                img_arr = np.array(img)

                def _is_solid_opaque(patch):
                    """11.09.2026: раньше проверяла ТОЛЬКО белый (r/g/b>215) —
                    живой баг на карточке 174182 (чёрный/тёмный маркетинговый
                    фон с частицами): bbox-углы были непрозрачно-чёрными, но
                    "белым" не считались, проверка не срабатывала, и
                    _already_transparent() принимал недовырезанный чёрный фон
                    как готовый результат, вообще не запуская rembg. Теперь
                    проверяем однородность (низкий разброс R/G/B) + непрозрачность,
                    независимо от того, тёмный это цвет или светлый."""
                    if patch.size == 0:
                        return False
                    r, g, b, a = patch[..., 0], patch[..., 1], patch[..., 2], patch[..., 3]
                    if float((a > 180).mean()) <= 0.7:
                        return False
                    std = float(np.std(np.stack([r, g, b], axis=-1).reshape(-1, 3), axis=0).mean())
                    return std < 12  # однородный (сплошной студийный/маркетинговый фон)

                patches = [
                    img_arr[by0:by0 + sz, bx0:bx0 + sz],
                    img_arr[by0:by0 + sz, bx1 - sz:bx1],
                    img_arr[by1 - sz:by1, bx0:bx0 + sz],
                    img_arr[by1 - sz:by1, bx1 - sz:bx1],
                ]
                solid_corners = sum(_is_solid_opaque(p) for p in patches)
                if solid_corners >= 3:
                    log.info(
                        f"PNG: bbox {bbox} — {solid_corners}/4 однородных угла → "
                        f"rembg (фон не вырезан внутри прозрачного канваса)"
                    )
                    return None
        except Exception as _e:
            log.debug(f"bbox corner check failed: {_e}")

        log.info(f"PNG: прозрачность {ratio:.1%} ≥ 40%, bbox углы ок — rembg не нужен")
        return img
    except Exception:
        pass
    return None


def _remove_floating_blobs(img: Image.Image) -> Image.Image:
    """Убирает изолированные блобы после rembg:
    1. Мелкие плавающие надписи/логотипы: < 1% главного блоба И дальше 30px.
    2. Маркетинговые панели спецификаций: крупный блоб (>5% главного) но
       светлый/белый (средняя яркость пикселей > 200) И дальше 20px от
       главного — характерно для marketing-фото «товар + панель со спеками»."""
    try:
        import numpy as np
        from scipy import ndimage as _nd
        img_arr = np.array(img)
        alpha = img_arr[..., 3].copy()
        mask = alpha > 30

        labeled, n = _nd.label(mask)
        if n <= 1:
            return img

        sizes = _nd.sum(mask, labeled, range(1, n + 1))
        main_label = int(np.argmax(sizes)) + 1
        main_size = int(sizes[main_label - 1])
        small_threshold = main_size * 0.01   # 1% — мелкие логотипы
        large_threshold = main_size * 0.05   # 5% — крупные панели

        main_mask = (labeled == main_label)
        dist = _nd.distance_transform_edt(~main_mask)

        # Сначала находим ВСЕ блобы-кандидаты на "крупная панель" (условие 2),
        # не удаляя их сразу. Комплект из нескольких белых физически разделённых
        # частей одного товара (напр. 2 вкладыша EarPods + Lightning-коннектор —
        # живой случай 25.09.2026, <артикул>) даёт 2+ таких кандидата —
        # это не панель спецификаций (та всегда ОДНА), а сам товар. Удаление
        # обеих частей оставляло один вкладыш и заваливало санитарную проверку
        # по aspect. Панель-эвристику применяем только когда кандидат ровно один.
        large_candidates = []
        for lbl in range(1, n + 1):
            if lbl == main_label:
                continue
            blob_mask = labeled == lbl
            blob_size = int(sizes[lbl - 1])
            min_dist = float(dist[blob_mask].min())
            if blob_size >= large_threshold and min_dist > 20:
                large_candidates.append(lbl)

        removed = 0
        for lbl in range(1, n + 1):
            if lbl == main_label:
                continue
            blob_mask = labeled == lbl
            blob_size = int(sizes[lbl - 1])
            min_dist = float(dist[blob_mask].min())

            # Условие 1: мелкий плавающий блоб (логотип/надпись)
            if blob_size < small_threshold and min_dist > 30:
                alpha[blob_mask] = 0
                removed += 1
                continue

            # Условие 2: крупный блоб, ДАЛЕКО (>20px) от главного — маркетинговая
            # панель спецификаций (обычно БЕЛАЯ, яркость > 200) ИЛИ вытянутая
            # декоративная полоса (напр. полоса цветовых вариантов товара сверху
            # фото — живой случай 02.09.2026, радужная полоса над зарядным
            # блоком AverMedia: не белая, но заметно уже/шире своей же bbox,
            # aspect > 4). НЕ убираем без этих условий — иначе режет легитимные
            # тёмные детали товара, отделившиеся от главного силуэта сегментацией
            # (живой репро в той же сессии: убрало 41% главного блоба на
            # AverMedia Elite GO GC313Pro — реальный кусок корпуса, не декор).
            if lbl in large_candidates and len(large_candidates) == 1:
                blob_rgb = img_arr[..., :3][blob_mask]
                avg_brightness = float(blob_rgb.mean())
                ys_b, xs_b = np.where(blob_mask)
                bw = int(xs_b.max() - xs_b.min() + 1)
                bh = int(ys_b.max() - ys_b.min() + 1)
                aspect = max(bw, bh) / max(1, min(bw, bh))
                is_white_panel = avg_brightness > 200
                is_bar_strip = aspect > 4
                if is_white_panel or is_bar_strip:
                    alpha[blob_mask] = 0
                    removed += 1
                    log.info(
                        f"Удалена изолированная панель/полоса: {blob_size}px "
                        f"({blob_size/main_size:.0%} главного), "
                        f"яркость={avg_brightness:.0f}, bbox={bw}x{bh} "
                        f"aspect={aspect:.1f}, dist={min_dist:.0f}px"
                    )
            elif lbl in large_candidates:
                log.info(
                    f"Крупный блоб {blob_size}px ({blob_size/main_size:.0%} главного) "
                    f"НЕ удалён: {len(large_candidates)} крупных кандидатов сразу "
                    f"— похоже на многокомпонентный товар, а не на одну панель"
                )

        if removed:
            log.info(f"_remove_floating_blobs: удалено {removed} блобов")
            result = img.copy()
            result.putalpha(Image.fromarray(alpha))
            return result
    except Exception as e:
        log.warning(f"_remove_floating_blobs failed: {e}")
    return img


def _bbox_with_buf(img: Image.Image, bbox, buf: int = 4) -> Image.Image:
    """Crop по bbox с буфером buf px — чтобы края корпуса не срезались.
    Без буфера getbbox() режет вплотную, и пиксели с alpha чуть < 30
    (угол камеры, закруглённый корпус) уходят за границу."""
    if not bbox:
        return img
    w, h = img.size
    x0 = max(0, bbox[0] - buf)
    y0 = max(0, bbox[1] - buf)
    x1 = min(w, bbox[2] + buf)
    y1 = min(h, bbox[3] + buf)
    return img.crop((x0, y0, x1, y1))


def remove_background(img_bytes: bytes) -> Image.Image | None:
    """Удаляет фон, возвращает RGBA с автокропом по bounding box товара.
    Если файл уже PNG с прозрачным фоном — skip rembg."""
    try:
        pre = _already_transparent(img_bytes)
        if pre is not None:
            import numpy as np
            from PIL import Image as PILImage
            alpha = pre.getchannel("A")
            alpha_arr = np.array(alpha)
            alpha_arr[alpha_arr < 30] = 0
            clean_alpha = PILImage.fromarray(alpha_arr)
            pre.putalpha(clean_alpha)
            pre = _remove_floating_blobs(pre)
            bbox = pre.getchannel("A").getbbox()
            pre = _bbox_with_buf(pre, bbox)
            log.info(f"Pre-transparent PNG, skip rembg + autocrop: {pre.size}")
            return pre

        from rembg import remove
        session = _get_session()
        try:
            result_bytes = remove(img_bytes, session=session)
        except Exception as e:
            # CUDA не смог выделить память (VRAM занята другими процессами
            # на рабочем столе) — не сдаёмся сразу на "белый фон", пробуем
            # ту же модель на CPU (медленнее, но вырезает нормально).
            log.warning(f"rembg CUDA failed, retry on CPU: {e}")
            result_bytes = remove(img_bytes, session=_get_session_cpu())
        img = Image.open(io.BytesIO(result_bytes)).convert("RGBA")

        # Порог прозрачности + чистка белых артефактов фона.
        import numpy as np
        from PIL import Image as PILImage
        rgba_arr = np.array(img)
        alpha_arr = rgba_arr[..., 3].copy()

        # 1. Порог: alpha < 8 → 0 (низкий порог сохраняет тонкие провода/кабели —
        #    rembg даёт им alpha ~8-14, порог 15 их срезал)
        alpha_arr[alpha_arr < 8] = 0

        # 2. Белые артефакты: пиксели одновременно светлые (RGB > 225) и
        #    полупрозрачные (alpha < 180) — остатки белого фона от rembg.
        r, g, b = rgba_arr[..., 0], rgba_arr[..., 1], rgba_arr[..., 2]
        is_near_white = (r > 225) & (g > 225) & (b > 225)
        is_semi = (alpha_arr > 0) & (alpha_arr < 180)
        alpha_arr[is_near_white & is_semi] = 0

        # 2b. Тёмная тень-кайма по контуру: rembg иногда сохраняет тонкую
        #     полосу тени исходного фона вдоль силуэта светлого товара —
        #     выглядит как тёмная "обводка". Послойно (по 1px) снимаем
        #     внешнее кольцо, если оно заметно темнее "внутренности" товара —
        #     так снимается кайма любой толщины (1-5px), не задевая
        #     легитимные тёмные детали в середине товара.
        try:
            import scipy.ndimage as _nd
            lum = 0.299 * r.astype(np.float32) + 0.587 * g.astype(np.float32) + 0.114 * b.astype(np.float32)
            alpha_before_ring = alpha_arr.copy()
            mask_before_ring = int((alpha_arr > 100).sum())
            total_removed = 0
            for _ in range(5):
                mask = alpha_arr > 100
                if mask.sum() <= 100:
                    break
                interior = _nd.binary_erosion(mask, iterations=1)
                if interior.sum() <= 100:
                    break
                interior_lum = float(np.median(lum[interior]))
                if interior_lum <= 120:  # светлый товар — тёмная кайма даст контраст
                    break
                ring = mask & ~interior
                dark_ring = ring & (lum < interior_lum * 0.45) & (lum < 100)
                n_dark = int(dark_ring.sum())
                if n_dark == 0 or n_dark >= mask.sum() * 0.08:
                    break
                alpha_arr[dark_ring] = 0
                total_removed += n_dark
            # Если "кайма" съела больше половины объекта — это не тонкая тень,
            # а сбой эвристики (светлая деталь товара принята за "внутренность",
            # а сам товар — за тёмную тень вокруг неё). Откатываем.
            if total_removed and int((alpha_arr > 100).sum()) < mask_before_ring * 0.5:
                log.warning(f"Тёмная кайма съела {total_removed} px (>50% объекта) → откат")
                alpha_arr = alpha_before_ring
                total_removed = 0
            if total_removed:
                log.info(f"Тёмная кайма по контуру: убрано {total_removed} px")
        except Exception as e:
            log.debug(f"Edge shadow cleanup failed: {e}")

        # 3. Белые связные области от края — только ТОНКИЕ РАМКИ фона (≤ 8px
        #    и тянутся ВДОЛЬ кромки кадра), а не детали товара, которые её
        #    касаются (антенна роутера, тонкий провод — тоже узкие, но
        #    задевают край лишь в одной точке, не растянуты вдоль него).
        #    Различаем по двум признакам:
        #      а) толщина — эрозия на 4 итерации (~8px): рамка исчезает,
        #         антенна/деталь товара выживает;
        #      б) доля касания одной стороны кадра — рамка тянется вдоль
        #         значительной части края (≥25% его длины), антенна касается
        #         края лишь на ширину самой себя (доли процента).
        #    Раньше проверялась только суммарная площадь (< 15%), из-за чего
        #    длинные тонкие детали (антенны роутера) съедались целиком —
        #    их площадь мала, но это не рамка фона (16.07).
        try:
            import scipy.ndimage as _nd
            white_opaque = (r > 230) & (g > 230) & (b > 230) & (alpha_arr > 128)
            labeled_w, _ = _nd.label(white_opaque)
            total_alpha = int((alpha_arr > 0).sum())
            hh, ww = alpha_arr.shape

            border_contact: dict[int, float] = {}  # label -> макс. доля касания одной стороны
            for edge_arr, edge_len in (
                (labeled_w[0, :], ww), (labeled_w[-1, :], ww),
                (labeled_w[:, 0], hh), (labeled_w[:, -1], hh),
            ):
                vals, counts = np.unique(edge_arr, return_counts=True)
                for v, c in zip(vals.tolist(), counts.tolist()):
                    if v == 0:
                        continue
                    frac_edge = c / edge_len
                    if frac_edge > border_contact.get(v, 0.0):
                        border_contact[v] = frac_edge

            if border_contact and total_alpha > 0:
                eroded = _nd.binary_erosion(white_opaque, iterations=4)
                rim_labels = {
                    lbl for lbl, frac_edge in border_contact.items()
                    if frac_edge >= 0.25
                    and not (eroded & (labeled_w == lbl)).any()
                }
                if rim_labels:
                    mask_remove = np.isin(labeled_w, list(rim_labels))
                    frac = mask_remove.sum() / alpha_arr.size
                    if frac < 0.15:  # < 15% — тонкая рамка, безопасно удалять
                        alpha_arr[mask_remove] = 0
                    else:
                        log.debug(f"Border flood-fill skipped: {frac:.0%} пикселей — фон продукта")
        except Exception:
            pass

        clean_alpha = PILImage.fromarray(alpha_arr)
        img.putalpha(clean_alpha)
        img = _remove_floating_blobs(img)

        bbox = img.getchannel("A").getbbox()
        # Для широких объектов с высоким preserve (мониторы, планшеты-дисплеи):
        # тайт-кроп по bbox режет тёмный беззел → оставляем полный размер.
        _asp_now = img.width / max(img.height, 1)
        _preserve_now = (bbox[2]-bbox[0]) * (bbox[3]-bbox[1]) / max(img.width*img.height, 1) if bbox else 0

        # Разреженное содержимое: bbox большой, но реальных пикселей мало
        # (рукоятка + корпус колонки разделены, даёт полупустой bbox).
        # Перцентильный кроп: убираем 3% крайних пикселей → плотная область.
        _alpha_arr2 = np.array(img.getchannel("A"))
        _opaque_ys, _opaque_xs = np.where(_alpha_arr2 > 30)
        _fill_ratio = 0.0
        if bbox and len(_opaque_xs) > 200:
            _bbox_area2 = (bbox[2]-bbox[0]) * (bbox[3]-bbox[1])
            _fill_ratio = len(_opaque_xs) / max(_bbox_area2, 1)
            if _fill_ratio < 0.25:
                _x0p = max(0, int(np.percentile(_opaque_xs, 3)) - 20)
                _x1p = min(img.width,  int(np.percentile(_opaque_xs, 97)) + 20)
                _y0p = max(0, int(np.percentile(_opaque_ys, 3)) - 20)
                _y1p = min(img.height, int(np.percentile(_opaque_ys, 97)) + 20)
                bbox = (_x0p, _y0p, _x1p, _y1p)
                _preserve_now = (_x1p-_x0p) * (_y1p-_y0p) / max(img.width*img.height, 1)
                log.info(f"Sparse content fill={_fill_ratio:.2%} → percentile bbox {bbox}, preserve={_preserve_now:.2%}")

        log.info(f"rembg crop: img={img.size} bbox={bbox} asp={_asp_now:.2f} preserve={_preserve_now:.2%} fill={_fill_ratio:.2%}")

        if _asp_now > 1.3 and _preserve_now > 0.75:
            pass  # широкий объект с чистым фоном — не кропаем
        else:
            if _preserve_now < 0.15:
                # Товар мал относительно холста — маркетинговое фото с большими
                # полями или спек-панели только что удалены _remove_floating_blobs.
                # Буфер по размеру самого bbox (не всего изображения): достаточно
                # для краёв корпуса, но не раздуваем пустоту вокруг.
                _bbox_w = (bbox[2] - bbox[0]) if bbox else img.width
                _bbox_h = (bbox[3] - bbox[1]) if bbox else img.height
                _buf = max(20, int(min(_bbox_w, _bbox_h) * 0.12))
            else:
                _buf = max(15, int(min(img.width, img.height) * 0.025))
            img = _bbox_with_buf(img, bbox, buf=_buf)

        # Санитарная проверка 1: aspect > 3.5 — экранный контент или баннер.
        # Порог 3.5 (не 2.0): наушники с микрофонной стрелой дают aspect 2.5-3.0 — не None.
        if img.width > 0 and img.height > 0:
            asp = max(img.width, img.height) / min(img.width, img.height)
            if asp > 3.5:
                log.info(f"rembg sanity: aspect {asp:.1f} > 3.5 → плохой вырез, None")
                return None

        # Санитарная проверка 2: несколько крупных объектов = составное фото.
        # Используем YOLO чтобы найти точный bbox нужного телефона,
        # кропаем оригинал по нему и прогоняем rembg заново — чисто.
        try:
            import scipy.ndimage as _nd
            alpha_arr2 = np.array(img.getchannel("A"))
            mask2 = alpha_arr2 > 30
            eroded = _nd.binary_erosion(mask2, iterations=4)
            labeled, n_comp = _nd.label(eroded)
            total_px = int(mask2.sum())
            sizes = {i: int((labeled == i).sum())
                     for i in range(1, n_comp + 1)
                     if total_px > 0 and int((labeled == i).sum()) >= total_px * 0.02}
            big_ids = list(sizes.keys())
            # При 2 объектах: второй должен быть >= 20% первого (не мелкий артефакт)
            is_composite = False
            if len(big_ids) >= 3:
                is_composite = True
            elif len(big_ids) == 2:
                s = sorted(sizes.values(), reverse=True)
                is_composite = s[1] >= s[0] * 0.20
            if is_composite:
                log.info(f"rembg: {len(big_ids)} объектов → запускаем YOLO")
                orig_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                cropped_orig = _yolo_crop_phone(orig_img)
                if cropped_orig is not None:
                    # Если YOLO-кроп < 20% оригинала — ложная цель (коннектор кабеля etc.)
                    crop_area = cropped_orig.width * cropped_orig.height
                    orig_area = orig_img.width * orig_img.height
                    if orig_area > 0 and crop_area / orig_area < 0.20:
                        log.info(f"YOLO кроп {crop_area/orig_area:.0%} < 20% оригинала — ложная цель, пропускаем")
                        cropped_orig = None
                if cropped_orig is not None:
                    # rembg заново на вырезанном телефоне
                    buf2 = io.BytesIO()
                    cropped_orig.save(buf2, format="JPEG", quality=92)
                    from rembg import remove as _remove
                    result2 = _remove(buf2.getvalue(), session=session)
                    img2 = Image.open(io.BytesIO(result2)).convert("RGBA")
                    alpha2 = np.array(img2.getchannel("A"))
                    alpha2[alpha2 < 30] = 0
                    from PIL import Image as _PIL
                    img2.putalpha(_PIL.fromarray(alpha2))
                    bbox2 = _PIL.fromarray(alpha2).getbbox()
                    img = _bbox_with_buf(img2, bbox2)
                    log.info(f"YOLO + rembg retry: {img.size}")
                else:
                    # YOLO не нашёл товар (не телефон/планшет) — доверяем rembg.
                    # Не режем по компонентам: наушники/гарнитуры/колонки состоят
                    # из нескольких частей (оголовье + чашки), и отбор largest-component
                    # необратимо обрезает их до одной чашки.
                    log.info(f"Composite: YOLO не нашёл → rembg-результат оставляем как есть ({len(big_ids)} компонент)")
        except Exception as e:
            log.warning(f"YOLO composite check failed: {e}")

        # Финальная проверка: если углы rembg-результата всё ещё однородно
        # закрашены+непрозрачны, значит birefnet не смог отделить товар от
        # фона. Раньше (до 11.09.2026) проверка была жёстко на БЕЛЫЙ цвет
        # (r/g/b > 215) — живой баг, найден на карточке 174182 (ОЗУ на
        # ЧЁРНОМ студийном фоне): углы оставались чёрными+непрозрачными,
        # доля "белых" была 0%, проверка не срабатывала вообще, и rembg
        # тихо "успешно" возвращал невырезанный чёрный прямоугольник.
        # Теперь цвет фона определяется по самим углам (медиана), а не
        # предполагается белым — ловит любой сплошной студийный фон.
        # Шаг 1: пробуем u2net (лучше на однотонных фонах).
        # Шаг 2: если u2net тоже не помог → flood-fill (последний resort).
        try:
            def _corners_solid_frac(image: Image.Image) -> float:
                a = np.array(image)
                hh, ww = a.shape[:2]
                sz = max(20, min(hh, ww) // 12)
                c = np.concatenate([
                    a[:sz, :sz].reshape(-1, 4),  a[:sz, -sz:].reshape(-1, 4),
                    a[-sz:, :sz].reshape(-1, 4), a[-sz:, -sz:].reshape(-1, 4),
                ])
                color = np.median(c[:, :3], axis=0)
                near = (np.all(np.abs(c[:, :3].astype(int) - color.astype(int)) <= 18, axis=1))
                return float((near & (c[:, 3] > 150)).mean())

            solid_frac = _corners_solid_frac(img)
            if solid_frac > 0.25:
                log.info(f"Углы однородные {solid_frac:.0%} (фон не вырезан) → пробуем u2net")
                try:
                    from rembg import remove as _remove
                    result_u2 = _remove(img_bytes, session=_get_session_u2net())
                    img_u2 = Image.open(io.BytesIO(result_u2)).convert("RGBA")
                    a2 = np.array(img_u2)
                    a2[..., 3][a2[..., 3] < 8] = 0
                    from PIL import Image as _PIL
                    img_u2.putalpha(_PIL.fromarray(a2[..., 3]))
                    bbox_u2 = _PIL.fromarray(a2[..., 3]).getbbox()
                    if bbox_u2:
                        img_u2 = _bbox_with_buf(img_u2, bbox_u2, buf=8)
                    sf2 = _corners_solid_frac(img_u2)
                    if sf2 < solid_frac:
                        log.info(f"u2net лучше: углы {sf2:.0%} < {solid_frac:.0%} → используем u2net")
                        img = img_u2
                        solid_frac = sf2
                    else:
                        log.info(f"u2net не помог ({sf2:.0%}) → flood-fill")
                except Exception as eu:
                    log.warning(f"u2net fallback failed: {eu}")

            if solid_frac > 0.25:
                log.info(f"Углы всё ещё однородные {solid_frac:.0%} → flood-fill (авто-цвет)")
                img = remove_solid_background(img_bytes, tolerance=25)
        except Exception as e:
            log.debug(f"Corner solid-color check failed: {e}")

        log.info(f"Background removed + autocrop: {img.size}")

        return img
    except Exception as e:
        log.warning(f"rembg failed: {e}")
        return None


def remove_white_background(img_bytes: bytes, tolerance: int = 20) -> Image.Image:
    """Flood-fill удаление белого/светлого фона от краёв изображения.
    Seeds — углы + середины всех четырёх сторон для полного покрытия.
    tolerance — допустимое отклонение от чисто-белого (255,255,255)."""
    return remove_solid_background(img_bytes, target=(255, 255, 255), tolerance=tolerance)


def _flatten_transparent_margins(img_bytes: bytes) -> bytes:
    """28.09.2026, живой баг на планках ОЗУ Apacer (<артикулы>):
    фото на WB — PNG с ПРОЗРАЧНЫМИ полями сверху/снизу и непрозрачным белым
    квадратом с товаром в центре. Детектор цвета фона по углам видел
    прозрачность, flood-fill снимал только её — белый квадрат оставался
    "белым боксом" на инфографике. Кладём такое фото на белый, тогда
    прозрачные поля и белая подложка становятся одним фоном."""
    try:
        im = Image.open(io.BytesIO(img_bytes))
        if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
            return img_bytes
        im = im.convert("RGBA")
        if im.getextrema()[3][0] == 255:
            return img_bytes
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        out = io.BytesIO()
        bg.convert("RGB").save(out, "PNG")
        return out.getvalue()
    except Exception as e:
        log.debug(f"_flatten_transparent_margins failed: {e}")
        return img_bytes


def remove_solid_background(img_bytes: bytes, target: tuple[int, int, int] | None = None,
                             tolerance: int = 20, _max_passes: int = 3) -> Image.Image:
    """Flood-fill удаление ОДНОРОДНОГО фона (белого/чёрного/серого/любого
    сплошного студийного) от краёв изображения. Многопроходная: некоторые
    фото имеют ДВА слоя фона (напр. белая рамка-канвас снаружи + отдельная
    цветная/тёмная подложка внутри, на которой стоит товар) - один проход
    снимает только внешний слой, второй пиксель-контур после кропа уже
    другого цвета. Живой баг, найден на карточке 174182 (планка ОЗУ:
    белая рамка WB + чёрная подложка) - после первого прохода (target=
    белый) результат оставался почти полностью непрозрачным (чёрная
    подложка), autocrop лишь чуть подрезал под неё. Теперь после каждого
    прохода при явном target=None заново проверяем цвет НОВЫХ углов и
    делаем ещё один проход, если он снова однородный - до _max_passes раз
    или пока проход перестаёт что-то менять.

    11.09.2026: remove_white_background жёстко предполагала белый фон
    (255,255,255) - при таймауте rembg фолбэк на "удаление белого" не
    находил белых пикселей вообще на чёрных фото и оставлял их нетронутыми.
    Если target не передан - определяем цвет фона САМИ по средним
    пикселям в 4 углах фото (студийные фото почти всегда имеют
    равномерный фон именно там, товар в кадре по центру)."""
    auto_detect = target is None
    img_bytes = _flatten_transparent_margins(img_bytes)
    result_img = _remove_solid_background_pass(img_bytes, target, tolerance)
    if auto_detect:
        for _ in range(_max_passes - 1):
            prev_size = result_img.size
            buf = io.BytesIO()
            result_img.save(buf, format="PNG")
            next_img = _remove_solid_background_pass(buf.getvalue(), None, tolerance)
            if next_img.size == prev_size:
                break  # проход ничего не срезал - дальше смысла нет
            result_img = next_img
    return result_img


def _remove_solid_background_pass(img_bytes: bytes, target: tuple[int, int, int] | None,
                                   tolerance: int) -> Image.Image:
    """Один проход flood-fill (см. remove_solid_background - многопроходная обёртка)."""
    import numpy as np
    from collections import deque

    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    arr = np.array(img)
    h, w = arr.shape[:2]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    if target is None:
        # Средний цвет в небольших патчах по 4 углам - устойчивее к шуму/
        # компрессионным артефактам, чем единичный пиксель. Только по
        # НЕПРОЗРАЧНЫМ пикселям - после первого прохода углы могут быть
        # уже частично прозрачными (alpha=0) от предыдущего среза.
        cs = max(4, min(h, w) // 40)
        patches = [
            arr[0:cs, 0:cs], arr[0:cs, w - cs:w],
            arr[h - cs:h, 0:cs], arr[h - cs:h, w - cs:w],
        ]
        corner_px = np.concatenate([p.reshape(-1, 4) for p in patches], axis=0)
        opaque_px = corner_px[corner_px[:, 3] > 150]
        if len(opaque_px) == 0:
            return img  # углы уже прозрачны - нечего снимать
        target = tuple(int(v) for v in opaque_px[:, :3].mean(axis=0))

    tr, tg, tb = target
    near_target = ((np.abs(r.astype(int) - tr) <= tolerance) &
                   (np.abs(g.astype(int) - tg) <= tolerance) &
                   (np.abs(b.astype(int) - tb) <= tolerance))
    near_white = near_target  # имя оставлено для минимальной диффы кода ниже

    visited = np.zeros((h, w), dtype=bool)
    to_remove = np.zeros((h, w), dtype=bool)

    # Seeds: 4 угла + середины сторон + каждый 8-й пиксель по периметру
    seeds = [
        (0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1),
        (0, w // 2), (h - 1, w // 2), (h // 2, 0), (h // 2, w - 1),
    ]
    step = max(1, min(h, w) // 20)
    for x in range(0, w, step):
        seeds += [(0, x), (h - 1, x)]
    for y in range(0, h, step):
        seeds += [(y, 0), (y, w - 1)]

    queue = deque()
    for sy, sx in seeds:
        sy = max(0, min(h - 1, sy))
        sx = max(0, min(w - 1, sx))
        if near_white[sy, sx] and not visited[sy, sx]:
            queue.append((sy, sx))
            visited[sy, sx] = True

    while queue:
        cy, cx = queue.popleft()
        to_remove[cy, cx] = True
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and near_white[ny, nx]:
                visited[ny, nx] = True
                queue.append((ny, nx))

    arr[to_remove, 3] = 0
    result = Image.fromarray(arr)
    bbox = result.getchannel("A").getbbox()
    if bbox:
        result = _bbox_with_buf(result, bbox, buf=8)
    log.info(f"White bg flood-fill: removed {int(to_remove.sum())} px, result {result.size}")
    return result


def _mask_coverage(rgba: Image.Image) -> float:
    """Доля непрозрачных пикселей внутри bbox маски (0..1)."""
    import numpy as np
    a = np.array(rgba.getchannel("A"))
    bbox = rgba.getchannel("A").getbbox()
    if not bbox:
        return 0.0
    x0, y0, x1, y1 = bbox
    region = a[y0:y1, x0:x1]
    if region.size == 0:
        return 0.0
    return float((region > 30).mean())


def _bbox_white_corners(rgba: Image.Image) -> float:
    """Доля непрозрачно-белых пикселей в углах bbox маски (0..1).
    Высокая → вырезка оставила белый прямоугольник фона."""
    import numpy as np
    arr = np.array(rgba)
    bbox = rgba.getchannel("A").getbbox()
    if not bbox:
        return 0.0
    x0, y0, x1, y1 = bbox
    hh, ww = y1 - y0, x1 - x0
    if hh < 10 or ww < 10:
        return 0.0
    sz = max(6, min(hh, ww) // 12)
    c = np.concatenate([
        arr[y0:y0 + sz, x0:x0 + sz].reshape(-1, 4),
        arr[y0:y0 + sz, x1 - sz:x1].reshape(-1, 4),
        arr[y1 - sz:y1, x0:x0 + sz].reshape(-1, 4),
        arr[y1 - sz:y1, x1 - sz:x1].reshape(-1, 4),
    ])
    return float(((c[:, 0] > 215) & (c[:, 1] > 215) & (c[:, 2] > 215) & (c[:, 3] > 150)).mean())


def _fill_enclosed_holes(rgba: Image.Image) -> Image.Image:
    """Замыкает «дыры» в альфа-маске, которые НЕ касаются края кадра.
    Настоящий фон всегда соединён с краем изображения — раз участок
    прозрачности со всех сторон окружён товаром, это не фон, а ошибочно
    вырезанный контент (типично — картинка на экране монитора, которую
    birefnet принял за отдельный объект). Не трогает честный фон вокруг
    товара, он всегда связан с краем кадра."""
    import numpy as np
    from scipy import ndimage as _nd
    arr = np.array(rgba)
    mask = arr[:, :, 3] > 30
    filled = _nd.binary_fill_holes(mask)
    holes = filled & ~mask
    if holes.any():
        arr[:, :, 3][holes] = 255
        return Image.fromarray(arr, "RGBA")
    return rgba


def _fill_panel_hull(rgba: Image.Image) -> Image.Image:
    """Достройка маски до выпуклой оболочки — только в зоне ПАНЕЛИ монитора.
    Панель — выпуклый прямоугольник, поэтому любые «выгрызы» в её маске
    (включая касающиеся края, которые не закрывает _fill_enclosed_holes) —
    ошибка вырезки, и их можно безусловно восстановить оболочкой.
    Зону подставки не трогаем: просветы фона у ножки — честный фон,
    оболочка залила бы их. Границы панели — непрерывный диапазон строк,
    где ширина маски ≥ 60% максимальной."""
    try:
        import numpy as np
        import cv2
        arr = np.array(rgba)
        mask = (arr[:, :, 3] > 30).astype(np.uint8)
        if not mask.any():
            return rgba
        row_w = mask.sum(axis=1)
        wide_rows = np.where(row_w >= 0.6 * row_w.max())[0]
        if wide_rows.size == 0:
            return rgba
        # Верх зоны — самая верхняя непустая строка маски (не первая «широкая»):
        # выгрыз у верхней кромки панели сужает верхние строки, и они выпадали
        # бы из зоны hull. Выше панели у монитора ничего нет — расширение вверх
        # безопасно. Вниз не расширяем (там подставка с честными просветами).
        y_top = int(np.where(row_w > 0)[0][0])
        y_bot = int(wide_rows[-1])
        sub = mask[y_top:y_bot + 1]
        pts = cv2.findNonZero(sub)
        if pts is None:
            return rgba
        hull = cv2.convexHull(pts)
        fill = np.zeros_like(sub)
        cv2.fillConvexPoly(fill, hull, 1)
        added = (fill == 1) & (sub == 0)
        n_added = int(added.sum())
        if not n_added:
            return rgba
        # Страховка: если оболочка добавляет больше 60% площади зоны панели —
        # маска слишком дырявая, hull может восстановить мусор; не рискуем.
        if n_added > 0.60 * sub.size:
            log.info(f"panel hull: добавка {n_added}px слишком велика — пропускаем")
            return rgba
        region = arr[y_top:y_bot + 1, :, 3]
        region[added] = 255
        arr[y_top:y_bot + 1, :, 3] = region
        log.info(f"panel hull: восстановлено {n_added}px выгрызов панели "
                 f"(строки {y_top}-{y_bot})")
        return Image.fromarray(arr, "RGBA")
    except Exception as e:
        log.warning(f"_fill_panel_hull failed: {e}")
        return rgba


def _monitor_cutout_problems(rgba: Image.Image) -> tuple[list[str], float, float]:
    """Метрики качества вырезки монитора: (список проблем, coverage, white_frac)."""
    problems = []
    cov = _mask_coverage(rgba)
    if cov < 0.60:
        problems.append(f"coverage {cov:.0%} (дыры в экране?)")
    wf = _bbox_white_corners(rgba)
    if wf > 0.25:
        problems.append(f"белые углы {wf:.0%} (белый квадрат?)")
    return problems, cov, wf


def _isnet_cutout(img_bytes: bytes) -> Image.Image | None:
    """Вырезка второй моделью (isnet-general-use) с минимальным пост-процессом:
    порог альфы + автокроп. Используется как второе мнение для мониторов,
    когда birefnet дал проблемную маску."""
    try:
        import numpy as np
        from rembg import remove as _remove
        result = _remove(img_bytes, session=_get_session_isnet())
        img = Image.open(io.BytesIO(result)).convert("RGBA")
        arr = np.array(img)
        arr[..., 3][arr[..., 3] < 8] = 0
        img = Image.fromarray(arr, "RGBA")
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = _bbox_with_buf(img, bbox, buf=8)
        return img
    except Exception as e:
        log.warning(f"isnet cutout failed: {e}")
        return None


def remove_background_monitor(img_bytes: bytes) -> Image.Image | None:
    """Монитор-безопасное вырезание (05.07, доработано 07.07). Мониторы —
    сплошные прямоугольники, поэтому известные беды детектятся метриками:

    1. «Дырявый экран»: birefnet вырезает изображение НА экране как фон —
       сначала лечим заливкой замкнутых дыр (_fill_enclosed_holes),
       остаточный детект — coverage bbox < 60%.
    2. «Белый квадрат»: белый прямоугольник фона остаётся в вырезке —
       детект: углы bbox непрозрачно-белые.

    Цепочка: birefnet → (если проблемы) isnet-general-use, вторая модель →
    (если и она не справилась) flood-fill от краёв → меньшее из зол."""
    rgba = remove_background(img_bytes)
    if rgba is None:
        return remove_white_background(img_bytes)

    rgba = _fill_enclosed_holes(rgba)
    rgba = _fill_panel_hull(rgba)
    problems, cov, wf = _monitor_cutout_problems(rgba)
    if not problems:
        return rgba

    # Кандидаты на «меньшее из зол», если ни один вариант не окажется чистым
    candidates: list[tuple[float, float, Image.Image]] = [(cov, wf, rgba)]

    # ── Второе мнение: isnet-general-use ──
    log.info(f"monitor cutout: {'; '.join(problems)} → пробуем isnet")
    isnet = _isnet_cutout(img_bytes)
    if isnet is not None:
        isnet = _fill_enclosed_holes(isnet)
        isnet = _fill_panel_hull(isnet)
        i_problems, i_cov, i_wf = _monitor_cutout_problems(isnet)
        if not i_problems:
            log.info(f"monitor cutout: isnet чистый (coverage {i_cov:.0%}) → используем isnet")
            return isnet
        candidates.append((i_cov, i_wf, isnet))
        log.info(f"monitor cutout: isnet тоже проблемный ({'; '.join(i_problems)}) → flood-fill")
    else:
        log.info("monitor cutout: isnet недоступен → flood-fill")

    # ── Последний resort: flood-fill от краёв ──
    try:
        ff = remove_white_background(img_bytes)
        ff_cov = _mask_coverage(ff)
        ff_wf = _bbox_white_corners(ff)
        if ff_cov >= 0.60 and ff_wf <= 0.25:
            return ff
        candidates.append((ff_cov, ff_wf, ff))
    except Exception as e:
        log.warning(f"monitor flood-fill fallback failed: {e}")

    # Меньшее из зол: максимальный coverage при минимуме белых углов
    candidates.sort(key=lambda c: (c[0] - c[1]), reverse=True)
    best_cov, best_wf, best = candidates[0]
    log.info(f"monitor cutout: чистого варианта нет, берём лучший "
             f"(coverage {best_cov:.0%}, белые углы {best_wf:.0%})")
    return best


def place_on_transparent(product_img: Image.Image,
                         zone_w: int, zone_h: int,
                         padding: int = 12,
                         extra_scale: float = 1.0) -> Image.Image:
    """
    Помещает товар (RGBA) в зону zone_w×zone_h с равным отступом.
    Реальный padding = max(переданный, 5% от меньшей стороны зоны) —
    это даёт визуальный «воздух» даже если PNG уже tight-cropped.
    Масштабирует чтобы максимально заполнить зону с сохранением пропорций.
    extra_scale < 1.0 — дополнительно уменьшает итоговый размер товара
    (центрируется в той же зоне, оставляя больше отступов).
    """
    canvas = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))

    src_w, src_h = product_img.size
    if src_w == 0 or src_h == 0:
        return canvas

    safe_padding = max(padding, int(min(zone_w, zone_h) * 0.05))
    max_w = zone_w - safe_padding * 2
    max_h = zone_h - safe_padding * 2

    fit_scale = min(max_w / src_w, max_h / src_h)
    fill_scale = max(max_w / src_w, max_h / src_h)
    # При сильном несовпадении пропорций (альбомное фото в портретной зоне,
    # напр. наушники с дугой K550 в высокой gaming-зоне) "fit" даёт товар
    # мелким с пустотой сверху/снизу. Разрешаем масштаб до 35% больше "fit",
    # но не больше "no_overflow_scale" — это гарантирует new_w<=zone_w и
    # new_h<=zone_h, т.е. изображение всегда центрируется без обрезки и без
    # сдвига к краю (раньше переполнение + прижатие к левому краю визуально
    # сдвигало товар вправо/влево относительно центра зоны).
    no_overflow_scale = min(zone_w / src_w, zone_h / src_h)
    scale = min(fill_scale, fit_scale * 1.35, no_overflow_scale) * extra_scale
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)

    img = product_img.resize((new_w, new_h), Image.LANCZOS)

    x = (zone_w - new_w) // 2
    y = (zone_h - new_h) // 2
    canvas.paste(img, (x, y), img)
    return canvas
