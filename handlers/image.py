import asyncio
import io
import json
import logging
import re
from dataclasses import replace as _dc_replace

import aiosqlite
from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message, PhotoSize

from database import UsersRepository, db_connect
from services.llm import get_llm
from services.card import (
    detect_category, get_priority_chars, build_context_from_search,
    clean_product_name, parse_product_info,
)
from services.search import find_product_images
from services.wb_content import get_wb_card_data
from services.image import make_infographic, make_second_slide, make_gaming_infographic, make_speakers_infographic, make_mice_infographic, make_ram_infographic, make_gpu_infographic, make_watch_infographic, make_signal_infographic, make_chair_infographic, make_simple_infographic, make_richcontent, pick_gaming_accent, pick_simple_accent, pick_bg_palette

# Категории со ВТОРЫМ слайдом (фото второго ракурса + одна характеристика +
# слоган). ВЫКЛЮЧЕНО 05.07 по итогам живого прогона мониторов: возвращаемся
# к схеме «одна инфографика + rich content». Код слайда сохранён
# (services/image/infographic_slide2.py) — включается добавлением категории.
SLIDE2_CATEGORIES: set[str] = set()
from utils.billing import save_cost
from utils.helpers import send_photo
from handlers.tasks import run_task
from prompts import (
    KEY_CHARS_PROMPT, GAMING_KEY_CHARS_PROMPT, GAMING_KEY_CHARS_PROMPTS,
    RICHCONTENT_TIPS_PROMPT, RICHCONTENT_SLOGAN_PROMPT, SLOGAN_PROMPT,
)

log = logging.getLogger(__name__)
router = Router()


# Gemini Vision-проверка фото в /batch. False — выключена (баланс Gemini пуст,
# вызовы ловят 429 и впустую жрут ~7-25с на карточку). Код Vision сохранён;
# когда пополнишь баланс Gemini — верни True. Цвет и так сторожат парсинг+RGB+URL.
USE_VISION = True


async def _get_user_style(user_id: int) -> str:
    async with db_connect() as db:
        return await UsersRepository(db).get_style(user_id)


async def _set_user_style(user_id: int, style: str) -> None:
    async with db_connect() as db:
        await UsersRepository(db).set_style(user_id, style)



async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


async def _get_cached_context(user_id: int) -> tuple[str, str, str, str, str, str, str] | None:
    """Возвращает (product, context, category, brand, model, color, color_en) из кэша или None."""
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT product, context, category, brand, model, color, color_en "
            "FROM card_cache WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
    if row:
        return (row["product"], row["context"], row["category"],
                row["brand"] or "", row["model"] or "",
                row["color"] or "", row["color_en"] or "")
    return None


def _parse_features(raw: str) -> list[tuple[str, str]]:
    """Парсит 'ЗАГОЛОВОК|значение' построчно."""
    features = []
    for line in raw.strip().splitlines():
        if "|" in line:
            t, v = line.split("|", 1)
            t, v = t.strip().upper(), v.strip()
            if t and v:
                features.append((t, v))
    return features[:4]


_TIP_MAX_WORDS = 14
_TIP_CUT_OFFENDERS = {"до", "на", "для", "с", "со", "из", "от", "по", "и", "а", "но", "в", "о", "об"}


def _trim_tip(value: str) -> str:
    """Страховка: если LLM выдал > 14 слов, режем по последнему «нормальному»
    слову (не предлог/союз) и ставим точку. Это гарантирует завершённость
    мысли и помещаемость в блок."""
    words = value.split()
    if len(words) <= _TIP_MAX_WORDS:
        return value
    cut = words[:_TIP_MAX_WORDS]
    while cut and cut[-1].lower().strip(".,!?:;") in _TIP_CUT_OFFENDERS:
        cut.pop()
    out = " ".join(cut).rstrip(".,!?:;—-")
    return out + "." if out else value


def _parse_tips(raw: str) -> list[tuple[str, str]]:
    """Парсит 'ЗАГОЛОВОК | описание' построчно."""
    tips = []
    for line in raw.strip().splitlines():
        if "|" in line:
            t, v = line.split("|", 1)
            t, v = t.strip(), v.strip()
            if t and v:
                tips.append((t, _trim_tip(v)))
    return tips[:2]


async def _extract_rich_slogan(description: str, product: str,
                                 tips: list[tuple[str, str]], llm) -> tuple[str, object]:
    """Слоган-цитата для третьего блока рич-контента (до 11 слов).
    tips передаются, чтобы слоган не дублировал их тематику."""
    avoid = ", ".join(t for t, _ in tips) if tips else ""
    hint = (
        f"[ТОВАР: {product}]\n"
        f"[УЖЕ ИСПОЛЬЗОВАНЫ В БЛОКАХ ВЫШЕ — НЕ ПОВТОРЯЙ ЭТИ ТЕМЫ: {avoid}]\n\n"
        f"Описание товара:\n"
    )
    resp = await llm.chat(hint + description, RICHCONTENT_SLOGAN_PROMPT)
    slogan = resp.text.strip().strip('"').strip("'")
    slogan = slogan.splitlines()[0] if slogan else ""
    import re as _re
    slogan = _re.sub(r'[.…]+$', '', slogan).strip()  # убираем trailing "..." и "…"
    slogan = slogan.replace("…", " ").strip()  # убираем "…" внутри текста
    slogan = _trim_to_complete_sentence(slogan, max_words=12)
    return slogan, resp


def _trim_to_complete_sentence(text: str, max_words: int) -> str:
    """Если в тексте есть точка/!/? — берём первое предложение целиком,
    но не длиннее max_words. Если оно длиннее или знака нет — режем по словам
    и отступаем с предлогов. Гарантирует завершённость мысли.

    Точка между цифрами («FreeLink 2.0», «1.5 ТБ») НЕ считается концом
    предложения — только точка перед пробелом или концом строки."""
    text = text.strip()
    if not text:
        return text
    import re
    # Точка/!/? за которой пробел или конец, и не между цифрами
    m = re.search(r"(?<!\d)[.!?](?=\s|$)", text)
    if m:
        first = text[:m.end()].strip()
        if len(first.split()) <= max_words:
            return first
    # Иначе обрезка по словам
    words = text.split()
    if len(words) <= max_words:
        return text if text.rstrip()[-1:] in ".!?" else text.rstrip(".,;:—- ") + "."
    cut = words[:max_words]
    while cut and cut[-1].lower().strip(".,!?:;") in _TIP_CUT_OFFENDERS:
        cut.pop()
    out = " ".join(cut).rstrip(".,!?:;—-")
    return (out + ".") if out else text


async def _extract_features(context: str, product: str, category: str,
                             llm, gaming: bool = False) -> tuple[list[tuple[str, str]], object]:
    """LLM извлекает ключевые характеристики для инфографики (обычно 4,
    но количество может отличаться — см. PRIORITY_CHARS по категории)."""
    # Логика выбора промпта вынесена в _build_features_prompt (общая с
    # объединённым _extract_visuals). GAMING_KEY_CHARS_PROMPTS — только для
    # категорий с выделенным промптом, иначе приоритетный список категории.
    prompt = _build_features_prompt(category, gaming)

    hint = f"[ЦЕЛЕВАЯ МОДЕЛЬ: {product}]\n\n"
    resp = await llm.chat(hint + context, prompt)
    features = _parse_features(resp.text)
    return features, resp


async def _extract_tips(context: str, product: str, llm) -> tuple[list[tuple[str, str]], object]:
    """LLM извлекает 3 преимущества для rich content."""
    hint = f"[ЦЕЛЕВАЯ МОДЕЛЬ: {product}]\n\n"
    resp = await llm.chat(hint + context, RICHCONTENT_TIPS_PROMPT)
    tips = _parse_tips(resp.text)
    return tips, resp


async def _extract_slogan(context: str, product: str, llm) -> tuple[str, object]:
    """LLM генерирует короткий слоган (до 6 слов) для шапки инфографики."""
    hint = f"[ЦЕЛЕВАЯ МОДЕЛЬ: {product}]\n\n"
    resp = await llm.chat(hint + context, SLOGAN_PROMPT)
    slogan = resp.text.strip().strip('"').strip("'").splitlines()[0] if resp.text.strip() else ""
    # Подстраховка — обрезаем если LLM проигнорировал лимит
    words = slogan.split()
    if len(words) > 6:
        slogan = " ".join(words[:6])
    return slogan, resp


def _build_features_prompt(category: str, gaming: bool) -> str:
    """Выбор промпта характеристик — общая логика для _extract_features
    и объединённого _extract_visuals."""
    if gaming and category in GAMING_KEY_CHARS_PROMPTS:
        return GAMING_KEY_CHARS_PROMPTS[category]
    priority = get_priority_chars(category)
    if priority:
        base_prompt = KEY_CHARS_PROMPT
        if len(priority) != 4:
            base_prompt = base_prompt.replace(
                "Выбери ровно 4 характеристики",
                f"Выбери ровно {len(priority)} характеристики",
            )
        return (
            f"КАТЕГОРИЯ ТОВАРА: {category}\n"
            f"ПРИОРИТЕТ (выбирай ровно {len(priority)} по порядку):\n{' → '.join(priority)}\n\n"
            + base_prompt
        )
    return KEY_CHARS_PROMPT


def _split_visual_sections(text: str) -> dict[str, str]:
    """Разбирает ответ объединённого промпта по маркерам [FEATURES]/[SLOGAN]/…"""
    import re as _re
    sections: dict[str, list[str]] = {}
    current = None
    marker_re = _re.compile(
        r'^\s*\**\[?\**(FEATURES|SLOGAN|TIPS|RICH_SLOGAN)\**\]?\**\s*:?\s*(.*)$',
        _re.IGNORECASE,
    )
    for line in text.splitlines():
        m = marker_re.match(line.strip())
        if m:
            current = m.group(1).upper()
            sections[current] = []
            rest = m.group(2).strip()
            if rest:
                sections[current].append(rest)
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


async def _extract_visuals(context: str, product: str, category: str, llm,
                            gaming: bool = False,
                            ) -> tuple[list[tuple[str, str]], str, list[tuple[str, str]], str, list]:
    """Features + slogan + tips + rich_slogan ОДНИМ вызовом LLM.

    Контекст (~2-4k токенов) раньше отправлялся 4 раза — по разу на каждую
    задачу; теперь один раз (экономия ~60% входных токенов и 3 сетевых
    раунда на карточку). При сломанном формате ответа — автоматический
    фолбэк на старые четыре отдельных вызова.
    Возвращает (features, slogan, tips, rich_slogan, [LLMResponse, ...])."""
    feat_prompt = _build_features_prompt(category, gaming)
    combined = (
        "Ты готовишь тексты для инфографики и rich-контента карточки товара.\n"
        "Выполни ЧЕТЫРЕ независимые задачи по правилам ниже. Фразы «выдай только…» "
        "внутри задач относятся к содержимому соответствующей секции ответа.\n\n"
        "═══ ЗАДАЧА 1 — секция [FEATURES] ═══\n" + feat_prompt +
        "\n\n═══ ЗАДАЧА 2 — секция [SLOGAN] ═══\n" + SLOGAN_PROMPT +
        "\n\n═══ ЗАДАЧА 3 — секция [TIPS] ═══\n" + RICHCONTENT_TIPS_PROMPT +
        "\n\n═══ ЗАДАЧА 4 — секция [RICH_SLOGAN] ═══\n" + RICHCONTENT_SLOGAN_PROMPT +
        "\nДОПОЛНИТЕЛЬНО для задачи 4: тема фразы НЕ должна повторять темы секции [TIPS] "
        "— выбери другой аспект товара.\n\n"
        "═══ ФОРМАТ ОТВЕТА (строго эти 4 маркера, каждый на своей строке) ═══\n"
        "[FEATURES]\n<строки ЗАГОЛОВОК|значение>\n"
        "[SLOGAN]\n<одна строка>\n"
        "[TIPS]\n<ровно 2 строки ЗАГОЛОВОК | текст>\n"
        "[RICH_SLOGAN]\n<одна строка>"
    )
    hint = f"[ЦЕЛЕВАЯ МОДЕЛЬ: {product}]\n\n"
    resp = await llm.chat(hint + context, combined)
    sections = _split_visual_sections(resp.text)

    features = _parse_features(sections.get("FEATURES", ""))
    slogan = sections.get("SLOGAN", "").strip().strip('"').strip("'")
    slogan = slogan.splitlines()[0].strip() if slogan else ""
    if len(slogan.split()) > 6:
        slogan = " ".join(slogan.split()[:6])
    tips = _parse_tips(sections.get("TIPS", ""))
    rich = sections.get("RICH_SLOGAN", "").strip().strip('"').strip("'")
    rich = rich.splitlines()[0].strip() if rich else ""
    import re as _re
    rich = _re.sub(r'[.…]+$', '', rich).strip().replace("…", " ").strip()
    rich = _trim_to_complete_sentence(rich, max_words=12) if rich else ""

    if features and slogan and tips:
        return features, slogan, tips, rich, [resp]

    # Фолбэк: LLM сломала формат — старые четыре вызова (надёжно, но дороже)
    log.warning(f"_extract_visuals: формат сломан (f={len(features)} s={bool(slogan)} "
                f"t={len(tips)}) — фолбэк на раздельные вызовы")
    features, feat_resp = await _extract_features(context, product, category, llm, gaming=gaming)
    slogan, slogan_resp = await _extract_slogan(context, product, llm)
    tips, tips_resp = await _extract_tips(context, product, llm)
    rich, rich_resp = await _extract_rich_slogan(context, product, tips, llm)
    return features, slogan, tips, rich, [resp, feat_resp, slogan_resp, tips_resp, rich_resp]


async def _run_image(message: Message, product: str, context: str, category: str,
                      brand: str = "", color: str = "", color_en: str = "",
                      override_img: bytes | None = None, raw_specs: str = ""):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    slide2_src: bytes | None = None
    if override_img is not None:
        # Пользователь приложил своё фото к /image — берём его напрямую,
        # поиск в интернете не запускаем.
        img_bytes = override_img
        progress = await message.answer(f"Использую твоё фото для: {product}...")
    else:
        # Фото товара — цвет (color_en) уводит поиск к нужной расцветке,
        # иначе берётся любой вариант модели.
        color_tag = f" ({color})" if color else ""
        progress = await message.answer(f"Ищу фото: {product}{color_tag}...")
        # Для slide2-категорий берём два ракурса: второй уйдёт на второй слайд
        n_photos = 2 if category in SLIDE2_CATEGORIES else 1
        photos = await find_product_images(
            product, n=n_photos, brand=brand, color=color, color_en=color_en, category=category,
        )
        img_bytes = photos[0] if photos else None
        slide2_src = photos[1] if len(photos) > 1 else None
    if not img_bytes:
        await progress.edit_text(f"Фото не найдено — генерирую без фото.")

    try:
        # ── Общие данные ──────────────────────────────────────────────────────
        await progress.edit_text(f"Подбираю характеристики...")
        user_style = await _get_user_style(user_id)
        features, slogan, tips, rich_slogan, vis_resps = await _extract_visuals(
            context, product, category, llm, gaming=(user_style == "gaming")
        )
        for _vr in vis_resps:
            await save_cost(user_id, "image_visuals", response=_vr)
        if rich_slogan:
            tips.append((rich_slogan, ""))

        # ── Инфографика ───────────────────────────────────────────────────────
        await progress.edit_text(f"Генерирую инфографику...")
        gaming_accent = None
        bg_palette = None
        if category == "Мыши":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_mice_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        elif category == "Оперативная память":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_ram_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        elif category == "Видеокарты":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_gpu_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        elif category == "Смарт-часы":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_watch_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        elif category == "Сетевое оборудование":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_signal_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        elif category == "Игровые кресла":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_chair_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        elif category == "Кроссовки":
            gaming_accent = await pick_simple_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_simple_infographic(
                product, features, tips, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
            )
        elif user_style == "gaming":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_gaming_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
            )
        elif user_style == "simple":
            gaming_accent = await pick_simple_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_simple_infographic(
                product, features, tips, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
            )
        elif category == "Акустика":
            gaming_accent = await pick_gaming_accent(product, features, llm, color_en=color_en, category=category)
            infographic, bg_warning = await make_speakers_infographic(
                product, features, img_bytes, llm=llm,
                brand=brand, category=category, accent=gaming_accent,
                raw_specs=raw_specs,
            )
        else:
            # Палитра выбирается заранее — второй слайд (если будет) получит
            # фон в той же гамме, что и первый.
            bg_palette = await pick_bg_palette(product, features, llm)
            infographic, bg_warning = await make_infographic(
                product, features, img_bytes, llm=llm, brand=brand, slogan=slogan,
                palette=bg_palette,
            )
        if infographic:
            caption = f"Инфографика: {product}"
            if bg_warning:
                caption += f"\n⚠️ {bg_warning}"
            await send_photo(message, infographic, caption)
        else:
            await message.answer("Инфографика не получилась.")

        # ── Второй слайд (пока только Мониторы): второй ракурс + одна
        #    главная характеристика + слоган из рич-контента ──────────────────
        if (category in SLIDE2_CATEGORIES and slide2_src and features
                and bg_palette is not None):
            await progress.edit_text(f"Генерирую второй слайд...")
            slide2 = await make_second_slide(
                product, features[0], rich_slogan or slogan, slide2_src,
                palette=bg_palette,
            )
            if slide2:
                await send_photo(message, slide2, f"Слайд 2: {product}")
            else:
                await message.answer("Второй слайд не получился.")

        # ── Rich content ──────────────────────────────────────────────────────
        await progress.edit_text(f"Генерирую rich content...")
        richcontent = await make_richcontent(
            product, features, tips, img_bytes, llm=llm,
            gaming_accent=gaming_accent, category=category,
        )
        if richcontent:
            await send_photo(message, richcontent, f"Rich content: {product}")
        else:
            await message.answer("Rich content не получился.")

        await progress.delete()

    except asyncio.CancelledError:
        await progress.edit_text("Отменено.")
    except Exception as e:
        log.error(f"Image failed for {product}: {e}", exc_info=True)
        await progress.edit_text(f"Ошибка: {e}")


def _split_article(raw: str) -> tuple[str, str]:
    """Артикул — все токены до первого кириллического слова.
    Остаток (начиная с первого русского слова) — название товара.
    Если кириллики нет совсем или с первого токена — артикула нет.
    Примеры: 'MR4 White v2 Акустика...' → ('MR4 White v2', 'Акустика...')
             'Logitech G435 Black'       → ('', 'Logitech G435 Black')"""
    import re as _re
    tokens = raw.split()
    cyrillic = _re.compile(r'[а-яёА-ЯЁ]')
    article_parts = []
    for i, tok in enumerate(tokens):
        if cyrillic.search(tok):
            product = " ".join(tokens[i:])
            return " ".join(article_parts), product
        article_parts.append(tok)
    return "", raw


async def _run_batch(message: Message, lines: list[str]):
    user_id = message.from_user.id
    llm = await get_llm(user_id)
    total = len(lines)

    progress = await message.answer(f"Батч: {total} товаров. Начинаю...")

    total_usd = 0.0
    total_gemini = 0
    failed_items: list[tuple[str, str]] = []  # (название, причина)

    # Excel-заполнение (инициализируем после первой карточки когда знаем категорию)
    from utils.excel_filler import ExcelBatchFiller
    excel_filler: ExcelBatchFiller | None = None
    # Категории, о которых уже сообщили пользователю в этом батче — не спамим
    # одним и тем же предупреждением на каждый товар (17.07).
    _warned_no_category = False
    _warned_categories: set[str] = set()

    # Кэш по модели (бренд+модель) — для пула «один товар, разные цвета»:
    # повторный поиск/описание/характеристики не делаем, только фото+инфографика.
    model_cache: dict[str, dict] = {}

    for i, raw_product in enumerate(lines, 1):
        raw = raw_product.strip()
        if not raw:
            continue

        article, product = _split_article(raw)

        # Разбираем бренд/модель/цвет один раз — чтобы определить, не цветовой
        # ли это вариант уже обработанной модели в этом батче.
        info = await parse_product_info(clean_product_name(product), llm)
        model_key = (
            f"{info.brand.strip().lower()}|{info.model.strip().lower()}"
            if info.brand.strip() and info.model.strip() else None
        )
        cached = model_cache.get(model_key) if model_key else None

        if cached:
            await progress.edit_text(f"[{i}/{total}] {product} (вариант цвета — данные переиспользую)...")
        else:
            await progress.edit_text(f"[{i}/{total}] {product}...")

        try:
            # Контекст и описание
            from services.card import generate_full_card
            if cached:
                result = _dc_replace(
                    cached["result"],
                    color=info.color, color_en=info.color_en,
                    article=article, llm_responses=[], exa_requests=0,
                )
            else:
                result = await generate_full_card(product, llm, desc_only=True, info=info)
                result.article = article

            # Отправляем описание
            from utils.helpers import reply_long, strip_markdown
            desc_msg = await message.answer(strip_markdown(result.description))

            # Сохраняем в кэш для /image
            async with db_connect() as db:
                await db.execute(
                    """INSERT OR REPLACE INTO card_cache
                       (user_id, product, brand, model, color, color_en, context, category, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (user_id, result.product, result.brand, result.model,
                     result.color, result.color_en, result.context, result.category)
                )
                await db.commit()

            for i, resp in enumerate(result.llm_responses):
                await save_cost(user_id, "batch_card", response=resp,
                                exa_requests=result.exa_requests if i == 0 else 0)
            total_usd += result.total_usd

            # Категория не определилась вообще — раньше карточка молча уходила
            # без Excel и без единого слова пользователю (17.07).
            if not result.category and not _warned_no_category:
                await message.answer(
                    f"⚠️ Категория не определена для «{result.product}» — "
                    f"характеристики будут по общему шаблону, Excel не подберётся."
                )
                _warned_no_category = True

            # Пытаемся получить РАБОЧИЙ Excel-филлер, пока его нет — не только
            # на первой карточке батча: если у первой категории шаблона не
            # нашлось, а у следующей — нашёлся, раньше батч так и оставался
            # без Excel до конца (17.07).
            if (excel_filler is None or not excel_filler.has_template) and result.category:
                if result.category not in _warned_categories:
                    candidate = ExcelBatchFiller(result.category)
                    candidate._load()
                    if candidate.has_template:
                        excel_filler = candidate
                        await message.answer(f"📊 Excel-шаблон найден для «{result.category}»")
                    else:
                        await message.answer(
                            f"⚠️ Скачать шаблон для категории «{result.category}» — "
                            f"{candidate.fail_reason}. Продолжаю без Excel."
                        )
                        _warned_categories.add(result.category)
            elif (excel_filler and excel_filler.has_template
                  and result.category != excel_filler.category
                  and result.category not in _warned_categories):
                await message.answer(
                    f"⚠️ «{result.product}»: категория «{result.category}» не совпадает "
                    f"с «{excel_filler.category}» этого батча — в Excel не попадёт "
                    f"(один Excel-файл = одна категория)."
                )
                _warned_categories.add(result.category)

            # Фото: сначала пробуем своё же фото с ВБ по этому артикулу (строгий
            # матч vendorCode — см. wb_content._find_card) — если карточка уже
            # заведена в этом кабинете, фото чистое (без вотермарков) и быстрее
            # любого веб-поиска. Не нашли строгое совпадение — обычный поиск.
            img_bytes = None
            if article:
                wb_data = await get_wb_card_data(article)
                if wb_data and wb_data["photos"]:
                    img_bytes = wb_data["photos"][0]
                    log.info(f"  -> фото с ВБ по артикулу {article!r} (строгий матч)")

            batch_photo_urls: list[str] = []
            if img_bytes is None:
                # Доп. ракурсы для rich content были отключены ниже, так что
                # искать их через Exa — лишний расход, поэтому только одно лучшее.
                # llm передан для DeepSeek-валидации URL — отсекает баннеры/другие модели
                # vision_validate=True — Gemini Vision смотрит само фото и проверяет
                #   цвет (если color_en задан) + отсекает коробки/коллажи
                photos = await find_product_images(
                    result.product, n=1, brand=result.brand,
                    color=result.color, color_en=result.color_en,
                    category=result.category,
                    llm=llm, vision_validate=USE_VISION,
                )
                img_bytes = photos[0] if photos else None
                # Снимок _last_urls сразу после поиска: атрибут функции глобален —
                # параллельная задача перезапишет его во время await ниже. Если фото
                # пришло с ВБ (поиск не вызывался) — оставляем [] (раньше сюда
                # попадали устаревшие URL от предыдущего товара).
                if img_bytes is not None:
                    batch_photo_urls = list(getattr(find_product_images, "_last_urls", []))
            extra_photos = []

            # ── Excel: генерируем характеристики по колонкам шаблона ──
            # Категория товара должна совпадать с категорией филлера — иначе
            # строка попадёт не в те колонки (несовпадение уже сообщено выше).
            if excel_filler and excel_filler.has_template and result.category == excel_filler.category:
                from services.card.generator import generate_packaging
                from services.search import product_search
                from prompts import WB_NAME_PROMPT

                # WB-название — регенерируем всегда (зависит от цвета варианта)
                wb_name_hint = (
                    f"Товар: {result.product}\n"
                    f"Бренд: {result.brand}\n\n"
                    f"{result.context[:800]}"
                )
                wb_name_resp = await llm.chat(wb_name_hint, WB_NAME_PROMPT)
                wb_name_lines = wb_name_resp.text.strip().strip('"').strip("'").splitlines()
                wb_name_raw = wb_name_lines[0] if wb_name_lines else result.product
                result.wb_name = wb_name_raw[:60]  # жёсткий обрез на случай если LLM нарушил лимит
                total_usd += wb_name_resp.usd
                await save_cost(user_id, "batch_wb_name", response=wb_name_resp)

                if cached:
                    # Цветовой вариант — характеристики/упаковка те же, что у анкора
                    pack_text = cached["pack_text"]
                    chars_text = cached["chars_text"]
                else:
                    # Спеки и упаковка — параллельно
                    specs_task = asyncio.create_task(
                        product_search.search_specs_context(result.product, category=result.category)
                    )
                    pack_task = asyncio.create_task(
                        generate_packaging(result.context, llm, product=result.product,
                                           category=result.category)
                    )
                    (specs_context, specs_count), (pack_text, pack_resp) = \
                        await asyncio.gather(specs_task, pack_task)

                    total_usd += pack_resp.usd
                    await save_cost(user_id, "batch_pack", response=pack_resp)

                    # Промпт строится из реальных колонок Excel — точное совпадение имён
                    excel_prompt = excel_filler.get_excel_chars_prompt()
                    if excel_prompt:
                        specs_block = (
                            "ХАРАКТЕРИСТИКИ С САЙТОВ РИТЕЙЛЕРОВ (DNS/Citilink/Ozon/Kaspi):\n"
                            + specs_context + "\n\n"
                        ) if specs_context else ""
                        chars_context = (
                            specs_block
                            + f"[ТОВАР: {result.product}]\n\n"
                            + f"ОПИСАНИЕ ТОВАРА (уже сгенерировано и проверено):\n{result.description}\n\n"
                            + result.context
                        )
                        chars_resp = await llm.chat(chars_context, excel_prompt)
                        chars_text = chars_resp.text.strip()
                        await save_cost(user_id, "batch_chars", response=chars_resp)
                        total_usd += chars_resp.usd
                    else:
                        chars_text = ""

                photo_url = batch_photo_urls[0] if batch_photo_urls else ""
                excel_filler.add_row(
                    result,
                    photo_url=photo_url,
                    chars_text=chars_text,
                    pack_text=pack_text,
                )

            # Если фото вообще не нашлось — пропускаем генерацию инфографики
            # и рич-контента целиком (экономия Gemini ~$0.078 + DeepSeek ~$0.02)
            if not img_bytes:
                color_tag = f" ({result.color})" if result.color else ""
                await message.answer(
                    f"⚠️ [{i}/{total}] {result.product}{color_tag}: "
                    f"фото товара не найдено — инфографика и rich content пропущены. "
                    f"Подбери фото вручную."
                )
                failed_items.append((f"{result.product}{color_tag}", "фото не найдено"))
                continue

            # Инфографика
            user_style = await _get_user_style(user_id)
            if cached:
                # Цветовой вариант — фичи/слоган/теги для рич-контента те же, что у анкора
                features = cached["features"]
                slogan = cached["slogan"]
                batch_gaming_accent = cached["gaming_accent"]
                tips = list(cached["tips"])
                rich_slogan = cached["rich_slogan"]
            else:
                features, slogan, tips, rich_slogan, vis_resps = await _extract_visuals(
                    result.context, result.product, result.category, llm,
                    gaming=(user_style == "gaming"),
                )
                for _vr in vis_resps:
                    await save_cost(user_id, "batch_visuals", response=_vr)
                    total_usd += _vr.usd

                batch_gaming_accent = None

            tips_for_cache = list(tips)

            if result.category == "Мыши":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_mice_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent, raw_specs=raw,
                )
            elif result.category == "Оперативная память":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_ram_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent, raw_specs=raw,
                )
            elif result.category == "Видеокарты":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_gpu_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent, raw_specs=raw,
                )
            elif result.category == "Смарт-часы":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_watch_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent, raw_specs=raw,
                )
            elif result.category == "Сетевое оборудование":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_signal_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent, raw_specs=raw,
                )
            elif result.category == "Игровые кресла":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_chair_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent, raw_specs=raw,
                )
            elif result.category == "Кроссовки":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_simple_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_simple_infographic(
                    result.product, features, tips, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent,
                )
            elif user_style == "gaming":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_gaming_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent,
                )
            elif user_style == "simple":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_simple_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_simple_infographic(
                    result.product, features, tips, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent,
                )
            elif result.category == "Акустика":
                if batch_gaming_accent is None:
                    batch_gaming_accent = await pick_gaming_accent(result.product, features, llm, color_en=result.color_en, category=result.category)
                infographic, bg_warning = await make_speakers_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, category=result.category,
                    accent=batch_gaming_accent,
                    raw_specs=raw,
                )
            else:
                infographic, bg_warning = await make_infographic(
                    result.product, features, img_bytes, llm=llm,
                    brand=result.brand, slogan=slogan,
                )
            color_tag = f" ({result.color})" if result.color else ""
            infographic_msg = None
            if infographic:
                caption = f"Инфографика: {result.product}{color_tag}"
                if bg_warning:
                    caption += f"\n⚠️ {bg_warning}. Белый фон."
                infographic_msg = await send_photo(message, infographic, caption)
                total_gemini += 1

            # Rich content
            if rich_slogan:
                tips.append((rich_slogan, ""))

            richcontent = await make_richcontent(
                result.product, features, tips, img_bytes,
                extra_photos=extra_photos, llm=llm,
                gaming_accent=batch_gaming_accent, category=result.category,
            )
            richcontent_msg = None
            if richcontent:
                richcontent_msg = await send_photo(message, richcontent, f"Rich content: {result.product}{color_tag}")
                total_gemini += 1

            # Сохраняем данные карточки для /altphoto — переподбор фото
            # после батча (ответом на инфографику или rich content)
            if infographic_msg or richcontent_msg:
                used_urls = batch_photo_urls
                async with db_connect() as db:
                    await db.execute(
                        """INSERT INTO batch_items
                           (user_id, message_id, richcontent_msg_id, product, brand, model,
                            color, color_en, category, features, slogan, tips, rich_slogan,
                            gaming_accent, user_style, used_photo_urls)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id,
                         infographic_msg.message_id if infographic_msg else None,
                         richcontent_msg.message_id if richcontent_msg else None,
                         result.product, result.brand, result.model,
                         result.color, result.color_en, result.category,
                         json.dumps(features, ensure_ascii=False),
                         slogan,
                         json.dumps(tips, ensure_ascii=False),
                         rich_slogan,
                         json.dumps(batch_gaming_accent) if batch_gaming_accent else None,
                         user_style,
                         json.dumps(used_urls, ensure_ascii=False))
                    )
                    await db.commit()

            # Кэшируем результат по модели — следующие цветовые варианты
            # этой же модели возьмут поиск/описание/характеристики отсюда
            if model_key and not cached:
                cache_entry = {
                    "result": result,
                    "features": features,
                    "slogan": slogan,
                    "gaming_accent": batch_gaming_accent,
                    "tips": tips_for_cache,
                    "rich_slogan": rich_slogan,
                }
                if excel_filler and excel_filler.has_template and result.category == excel_filler.category:
                    cache_entry["pack_text"] = pack_text
                    cache_entry["chars_text"] = chars_text
                model_cache[model_key] = cache_entry

        except asyncio.CancelledError:
            await progress.edit_text(f"Отменено на [{i}/{total}] {product}")
            return
        except Exception as e:
            log.error(f"Batch item failed '{product}': {e}", exc_info=True)
            err_short = str(e)[:120]
            await message.answer(f"[{i}/{total}] Ошибка: {product}\n{err_short}")
            failed_items.append((product, f"ошибка: {err_short}"))
            continue

    ok_count = total - len(failed_items)
    await progress.edit_text(
        f"Готово! {ok_count}/{total} товаров обработано.\n"
        f"LLM: ${total_usd:.4f}\n"
        f"Gemini: {total_gemini} изображений"
    )

    # Отдельное сообщение о проваленных товарах
    if failed_items:
        lines = [f"🔴 Не удалось обработать: {len(failed_items)} из {total}\n"]
        for idx, (name, reason) in enumerate(failed_items, 1):
            lines.append(f"{idx}. {name}\n    └ {reason}")
        await message.answer("\n".join(lines))

    # Отправляем Excel если был заполнен
    if excel_filler and excel_filler.has_template and excel_filler.rows_filled > 0:
        xlsx_bytes = excel_filler.get_bytes()
        if xlsx_bytes:
            from aiogram.types import BufferedInputFile
            fname = f"wb_{excel_filler.category}_{excel_filler.rows_filled}шт.xlsx"
            await message.answer_document(
                BufferedInputFile(xlsx_bytes, filename=fname),
                caption=f"📊 WB шаблон заполнен: {excel_filler.rows_filled} товаров"
            )


async def _run_excel_batch(message: Message, lines: list[str]):
    """Заполняет WB Excel-шаблон по списку моделей: описание, характеристики,
    упаковка, WB-название, фото — без генерации инфографики и rich content."""
    user_id = message.from_user.id
    llm = await get_llm(user_id)
    total = len(lines)

    progress = await message.answer(f"Excel: {total} товаров. Начинаю...")

    total_usd = 0.0
    failed_items: list[tuple[str, str]] = []

    from utils.excel_filler import ExcelBatchFiller
    excel_filler: ExcelBatchFiller | None = None

    # Кэш по модели (бренд+модель) — для пула «один товар, разные цвета»:
    # повторный поиск/описание/характеристики не делаем, только фото.
    model_cache: dict[str, dict] = {}

    for i, raw_product in enumerate(lines, 1):
        raw = raw_product.strip()
        if not raw:
            continue

        article, product = _split_article(raw)

        info = await parse_product_info(clean_product_name(product), llm)
        model_key = (
            f"{info.brand.strip().lower()}|{info.model.strip().lower()}"
            if info.brand.strip() and info.model.strip() else None
        )
        cached = model_cache.get(model_key) if model_key else None

        if cached:
            await progress.edit_text(f"[{i}/{total}] {product} (вариант цвета — данные переиспользую)...")
        else:
            await progress.edit_text(f"[{i}/{total}] {product}...")

        try:
            from services.card import generate_full_card
            if cached:
                result = _dc_replace(
                    cached["result"],
                    color=info.color, color_en=info.color_en,
                    article=article, llm_responses=[], exa_requests=0,
                )
            else:
                result = await generate_full_card(product, llm, desc_only=True, info=info)
                result.article = article

            async with db_connect() as db:
                await db.execute(
                    """INSERT OR REPLACE INTO card_cache
                       (user_id, product, brand, model, color, color_en, context, category, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (user_id, result.product, result.brand, result.model,
                     result.color, result.color_en, result.context, result.category)
                )
                await db.commit()

            for i, resp in enumerate(result.llm_responses):
                await save_cost(user_id, "excel_card", response=resp,
                                exa_requests=result.exa_requests if i == 0 else 0)
            total_usd += result.total_usd

            # Excel-филлер инициализируем по категории первого успешного товара
            if excel_filler is None:
                if not result.category:
                    await message.answer(
                        f"⚠️ [{i}/{total}] {result.product}: категория не определена — пропущен."
                    )
                    failed_items.append((result.product, "категория не определена"))
                    continue
                excel_filler = ExcelBatchFiller(result.category)
                excel_filler._load()
                if not excel_filler.has_template:
                    await progress.edit_text(
                        f"⚠️ Скачать шаблон для категории «{result.category}» — "
                        f"{excel_filler.fail_reason}. Отменяю."
                    )
                    return
                await message.answer(f"📊 Excel-шаблон найден для «{result.category}»")

            # Один Excel = одна категория — товары другой категории пропускаем,
            # чтобы не записать данные в чужие колонки шаблона
            if result.category != excel_filler.category:
                await message.answer(
                    f"⚠️ [{i}/{total}] {result.product}: категория «{result.category}» "
                    f"не совпадает с «{excel_filler.category}» — пропущен (один Excel = одна категория)."
                )
                failed_items.append((result.product, f"другая категория: {result.category}"))
                continue

            # Фото для колонки «Фото» (один ракурс — для инфографики не нужен)
            photos = await find_product_images(
                result.product, n=1, brand=result.brand,
                color=result.color, color_en=result.color_en,
                category=result.category,
                llm=llm, vision_validate=USE_VISION,
            )
            # _last_urls читаем СРАЗУ после вызова: это атрибут функции (глобальное
            # состояние), параллельная задача перезапишет его во время await ниже
            photo_urls_snapshot = list(getattr(find_product_images, "_last_urls", []))
            if not photos:
                color_tag = f" ({result.color})" if result.color else ""
                await message.answer(
                    f"⚠️ [{i}/{total}] {result.product}{color_tag}: фото товара не найдено."
                )

            # WB-название — регенерируем всегда (зависит от цвета варианта)
            from prompts import WB_NAME_PROMPT

            wb_name_hint = (
                f"Товар: {result.product}\n"
                f"Бренд: {result.brand}\n\n"
                f"{result.context[:800]}"
            )
            wb_name_resp = await llm.chat(wb_name_hint, WB_NAME_PROMPT)
            wb_name_raw = wb_name_resp.text.strip().strip('"').strip("'").splitlines()[0]
            result.wb_name = wb_name_raw[:60]
            total_usd += wb_name_resp.usd
            await save_cost(user_id, "excel_wb_name", response=wb_name_resp)

            if cached:
                # Цветовой вариант — характеристики/упаковка те же, что у анкора
                pack_text = cached["pack_text"]
                chars_text = cached["chars_text"]
            else:
                # Спеки, упаковка — параллельно (как в /batch)
                from services.card.generator import generate_packaging
                from services.search import product_search

                specs_task = asyncio.create_task(
                    product_search.search_specs_context(result.product, category=result.category)
                )
                pack_task = asyncio.create_task(
                    generate_packaging(result.context, llm, product=result.product,
                                       category=result.category)
                )
                (specs_context, specs_count), (pack_text, pack_resp) = \
                    await asyncio.gather(specs_task, pack_task)

                total_usd += pack_resp.usd
                await save_cost(user_id, "excel_pack", response=pack_resp)

                excel_prompt = excel_filler.get_excel_chars_prompt()
                if excel_prompt:
                    specs_block = (
                        "ХАРАКТЕРИСТИКИ С САЙТОВ РИТЕЙЛЕРОВ (DNS/Citilink/Ozon/Kaspi):\n"
                        + specs_context + "\n\n"
                    ) if specs_context else ""
                    chars_context = (
                        specs_block
                        + f"[ТОВАР: {result.product}]\n\n"
                        + f"ОПИСАНИЕ ТОВАРА (уже сгенерировано и проверено):\n{result.description}\n\n"
                        + result.context
                    )
                    chars_resp = await llm.chat(chars_context, excel_prompt)
                    chars_text = chars_resp.text.strip()
                    await save_cost(user_id, "excel_chars", response=chars_resp)
                    total_usd += chars_resp.usd
                else:
                    chars_text = ""

            photo_url = photo_urls_snapshot[0] if photo_urls_snapshot else ""
            excel_filler.add_row(
                result,
                photo_url=photo_url,
                chars_text=chars_text,
                pack_text=pack_text,
            )

            if model_key and not cached:
                model_cache[model_key] = {
                    "result": result,
                    "pack_text": pack_text,
                    "chars_text": chars_text,
                }

        except asyncio.CancelledError:
            await progress.edit_text(f"Отменено на [{i}/{total}] {product}")
            return
        except Exception as e:
            log.error(f"Excel batch item failed '{product}': {e}", exc_info=True)
            err_short = str(e)[:120]
            await message.answer(f"[{i}/{total}] Ошибка: {product}\n{err_short}")
            failed_items.append((product, f"ошибка: {err_short}"))
            continue

    ok_count = total - len(failed_items)
    await progress.edit_text(
        f"Готово! {ok_count}/{total} товаров обработано.\n"
        f"LLM: ${total_usd:.4f}"
    )

    if failed_items:
        out_lines = [f"🔴 Не удалось обработать: {len(failed_items)} из {total}\n"]
        for idx, (name, reason) in enumerate(failed_items, 1):
            out_lines.append(f"{idx}. {name}\n    └ {reason}")
        await message.answer("\n".join(out_lines))

    if excel_filler and excel_filler.has_template and excel_filler.rows_filled > 0:
        xlsx_bytes = excel_filler.get_bytes()
        if xlsx_bytes:
            from aiogram.types import BufferedInputFile
            fname = f"wb_{excel_filler.category}_{excel_filler.rows_filled}шт.xlsx"
            await message.answer_document(
                BufferedInputFile(xlsx_bytes, filename=fname),
                caption=f"📊 WB шаблон заполнен: {excel_filler.rows_filled} товаров"
            )
    else:
        await message.answer("Excel не заполнен — нет успешно обработанных товаров.")


# ── Хендлеры ──────────────────────────────────────────────────────────────────

@router.message(Command("style"))
async def cmd_style(message: Message):
    """Переключение стиля инфографики: /style default | /style gaming | /style simple"""
    user_id = message.from_user.id
    parts   = (message.text or "").split(None, 1)
    arg     = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg:
        current = await _get_user_style(user_id)
        labels  = {
            "default": "классический (пастельный фон)",
            "gaming": "игровой (тёмный + яркий акцент)",
            "simple": "simple (цветная шапка, характеристики снизу)",
        }
        await message.answer(
            f"Текущий стиль инфографики: {labels.get(current, current)}\n\n"
            f"/style default — классический\n"
            f"/style gaming — игровой\n"
            f"/style simple — для нетехнических товаров (расходники и т.п.)"
        )
        return

    if arg in ("default", "classic", "обычный"):
        await _set_user_style(user_id, "default")
        await message.answer("Стиль: классический (пастельный фон, светлые блоки).")
    elif arg == "gaming":
        await _set_user_style(user_id, "gaming")
        await message.answer("Стиль: игровой (тёмный фон, яркий акцент, pill-блоки).")
    elif arg == "simple":
        await _set_user_style(user_id, "simple")
        await message.answer("Стиль: simple (цветная шапка, фото товара, бейджи доверия, характеристики снизу).")
    else:
        await message.answer("Неизвестный стиль. Доступно:\n/style default\n/style gaming\n/style simple")


@router.message(Command("image"))
async def cmd_image(message: Message, bot: Bot):
    user_id = message.from_user.id
    # Команда может прийти как обычным текстом, так и в ПОДПИСИ к фото —
    # для фото-сообщения message.text == None, аргумент лежит в caption.
    text = message.text or message.caption or ""
    parts = text.split(None, 1)
    product_arg = parts[1].strip() if len(parts) > 1 else ""

    # Прикреплённое фото — используем напрямую, без поиска в интернете.
    override_img: bytes | None = None
    if message.photo:
        best_photo = max(message.photo, key=lambda p: p.file_size or 0)
        try:
            override_img = await _download_photo(bot, best_photo)
        except Exception as e:
            await message.answer(f"Не удалось загрузить прикреплённое фото: {e}")
            return

    if product_arg:
        # Явно указан товар — нормализуем через LLM
        llm = await get_llm(user_id)
        from services.card import parse_product_info, clean_product_name
        product_clean = clean_product_name(product_arg)
        info = await parse_product_info(product_clean, llm)
        category = detect_category(info.full_name) or detect_category(product_arg)
        if not category:
            # Текст команды не содержит слова категории (например, без
            # «Мышь» в названии) — пробуем взять категорию из последней
            # карточки того же бренда (она была определена при /card или
            # /batch, где исходная строка содержала слово категории).
            cached = await _get_cached_context(user_id)
            if cached and cached[3].lower() == info.brand.lower() and cached[2]:
                category = cached[2]
        progress = await message.answer(f"Ищу информацию: {info.full_name}...")
        # Контекст ищем по full_name — Exa лучше находит без RAM/памяти
        # raw_specs = полная строка — авторитет для RAM/цвета в характеристиках
        context, exa_count, search_resps = await build_context_from_search(
            info.full_name, category, llm, raw_specs=product_arg
        )
        for r in search_resps:
            await save_cost(user_id, "image_search", response=r)
        # Для поиска фото используем search_name (с памятью) — точнее
        search_name = info.search_name if info.search_name else info.full_name
        await progress.delete()
        run_task(user_id, _run_image(
            message, search_name, context, category,
            brand=info.brand, color=info.color, color_en=info.color_en,
            override_img=override_img, raw_specs=product_arg,
        ))
    else:
        # Берём из кэша последней карточки
        cached = await _get_cached_context(user_id)
        if not cached:
            await message.answer(
                "Нет сохранённой карточки.\n"
                "Сначала сделай /card или укажи товар:\n"
                "/image Название товара"
            )
            return
        product, context, category, brand, _model, color, color_en = cached
        run_task(user_id, _run_image(
            message, product, context, category, brand=brand,
            color=color, color_en=color_en, override_img=override_img,
        ))


@router.message(Command("batch"))
async def cmd_batch(message: Message):
    text = message.text or ""
    raw_lines = [l.strip() for l in text.split("\n")[1:] if l.strip()]

    if not raw_lines:
        await message.answer(
            "Отправь список товаров — каждый с новой строки:\n\n"
            "/batch\n"
            "Samsung Galaxy A55\n"
            "Oscal Flat 2\n"
            "Xiaomi Redmi Note 13\n\n"
            "/cancel — остановить"
        )
        return

    run_task(message.from_user.id, _run_batch(message, raw_lines))


@router.message(Command("excel"))
async def cmd_excel(message: Message):
    text = message.text or ""
    raw_lines = [l.strip() for l in text.split("\n")[1:] if l.strip()]

    if not raw_lines:
        await message.answer(
            "Отправь список товаров — каждый с новой строки:\n\n"
            "/excel\n"
            "Samsung Galaxy A55\n"
            "Oscal Flat 2\n"
            "Xiaomi Redmi Note 13\n\n"
            "Заполню Excel-шаблон (описание, характеристики, упаковка, "
            "WB-название, фото) без инфографики и rich content.\n"
            "/cancel — остановить"
        )
        return

    run_task(message.from_user.id, _run_excel_batch(message, raw_lines))
