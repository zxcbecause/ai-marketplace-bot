"""/rarity — набор карточки WB (N слайдов 3:4 + rich content) СТРОГО на
оригинальных фото пользователя (без поиска в интернете), с распределением
подтверждённых характеристик по слайдам без повторов. N = сколько уникальных
фото прислал пользователь — слайд 1 всегда «топовые преимущества», слайды
2..N — по одному отдельному преимуществу на фото (правка 17.07.2026).

Слайды рендерятся по фоновому шаблону пользователя (см.
services/image/rarity_templates.py — плашки заголовка/характеристик и
ватермарка магазина уже впечатаны в саму картинку фона, рендерер только
вставляет вырезанное фото товара и текст, см.
services/image/infographic_rarity.py). Фон (пока 1 штука, пул расширяемый) и
шрифт (12 семей, подбор по тематике категории — см. CATEGORY_FONT_MOOD в
services/image/fonts.py) бот выбирает случайно на каждый запуск, без выбора
пользователя.

УПРОЩЕНИЯ V1 (сознательно, из-за объёма фичи):
- все слайды используют ТОТ ЖЕ фоновый шаблон (гарантирует единый фон/стиль),
  просто с другим фото и другой характеристикой — композиция (расположение
  зон) между слайдами не варьируется, в отличие от точной формулировки
  исходного ТЗ пользователя.
- rich content (800×500) пока НЕ на пользовательском шаблоне — под него фон
  не присылали, поэтому используется старый make_richcontent (локальный
  градиент, без gaming-акцента), с миниатюрами из фото 2-3 если они есть.
- альбом собирается по media_group_id с debounce 1.5с — если фото придут не
  по порядку (редкий краевой случай Telegram), подпись может не попасть в
  первое обработанное фото.
"""
import asyncio
import logging

from aiogram import Router, Bot
from aiogram.types import Message, PhotoSize

from services.llm import get_llm
from services.card import detect_category, build_context_from_search, clean_product_name, parse_product_info
from services.image import make_richcontent
from services.image.fonts import pick_random_rarity_family, set_rarity_family, reset_rarity_family
from services.image.rarity_templates import pick_random_rarity_template
from services.image.infographic_rarity import build_rarity_slide
from utils.billing import save_cost
from utils.helpers import send_photo
from handlers.tasks import run_task
from prompts import RARITY_CONTENT_PLAN_PROMPT

log = logging.getLogger(__name__)
router = Router()

_DEBOUNCE_SEC = 1.5

# media_group_id (или синтетический "single-<msg_id>") → {photos, message, caption, task}
_ALBUMS: dict[str, dict] = {}


async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
    file = await bot.get_file(photo.file_id)
    import io
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


def _parse_plan_lines(raw: str, max_n: int) -> list[tuple[str, str]]:
    """Парсит 'ЗАГОЛОВОК|значение' построчно, без капа на 4 (в отличие от
    _parse_features из handlers/image.py — тут нужны разные лимиты по секциям)."""
    out = []
    for line in raw.strip().splitlines():
        if "|" in line:
            t, v = line.split("|", 1)
            t, v = t.strip().upper(), v.strip()
            if t and v:
                out.append((t, v))
    return out[:max_n]


def _split_plan_sections(text: str) -> dict[str, str]:
    import re
    sections: dict[str, list[str]] = {}
    current = None
    marker_re = re.compile(
        r'^\s*\**\[?\**(SLIDE1|EXTRA_FEATURES|RICH_FEATURES|RICH_TIPS)\**\]?\**\s*:?\s*(.*)$',
        re.IGNORECASE,
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


async def _flush_album(group_id: str):
    await asyncio.sleep(_DEBOUNCE_SEC)
    entry = _ALBUMS.pop(group_id, None)
    if not entry:
        return
    await _start_rarity(entry["message"], entry["photos"], entry["caption"])


@router.message(lambda m: m.photo is not None and (
    (m.caption or "").strip().lower().startswith("/rarity")
    or (m.media_group_id and m.media_group_id in _ALBUMS)
))
async def handle_rarity_photo(message: Message, bot: Bot):
    gid = message.media_group_id or f"single-{message.message_id}"
    best = max(message.photo, key=lambda p: p.file_size or 0)
    try:
        photo_bytes = await _download_photo(bot, best)
    except Exception as e:
        await message.answer(f"Не удалось загрузить фото: {e}")
        return

    if gid not in _ALBUMS:
        caption = (message.caption or "").strip()
        parts = caption.split(None, 1)
        product_arg = parts[1].strip() if len(parts) > 1 else ""
        _ALBUMS[gid] = {"photos": [], "message": message, "caption": product_arg, "task": None}

    entry = _ALBUMS[gid]
    entry["photos"].append(photo_bytes)
    if entry["task"]:
        entry["task"].cancel()
    entry["task"] = asyncio.create_task(_flush_album(gid))


async def _start_rarity(message: Message, photos: list[bytes], product_arg: str):
    user_id = message.from_user.id

    if not product_arg:
        await message.answer(
            "🔴 Название товара не найдено — пришли фото (можно альбомом) с "
            "подписью «/rarity Название товара» на ПЕРВОМ фото."
        )
        return
    if not photos:
        await message.answer("🔴 Нужно хотя бы одно оригинальное фото товара.")
        return

    # Сколько уникальных фото дали — столько слайдов и будет (правка 17.07.2026).
    run_task(user_id, _generate_rarity(message, photos, product_arg))


async def _generate_rarity(message: Message, photos: list[bytes], product_arg: str):
    user_id = message.from_user.id
    llm = await get_llm(user_id)
    progress = await message.answer(f"Rarity: анализирую «{product_arg}» по официальным характеристикам...")

    font_token = None
    try:
        product_clean = clean_product_name(product_arg)
        info = await parse_product_info(product_clean, llm)
        category = detect_category(info.full_name) or detect_category(product_arg) or ""
        product = info.full_name

        # Шрифт подбирается ПО ТЕМАТИКЕ категории (см. CATEGORY_FONT_MOOD в
        # services/image/fonts.py) — независимо от рандомного gaming/simple
        # визуального стиля ниже и от сохранённого /style пользователя
        # (тот используется только в /image, сюда не заглядываем — у /rarity
        # своя, полностью отдельная рандомизация).
        font_family = pick_random_rarity_family(category)
        font_token = set_rarity_family(font_family)

        context, exa_count, search_resps = await build_context_from_search(
            product, category, llm, raw_specs=product_arg
        )
        for r in search_resps:
            await save_cost(user_id, "rarity_search", response=r)

        await progress.edit_text("Rarity: распределяю преимущества по слайдам...")
        plan_resp = await llm.chat(f"[ТОВАР: {product}]\n\n{context}", RARITY_CONTENT_PLAN_PROMPT)
        await save_cost(user_id, "rarity_plan", response=plan_resp)
        sections = _split_plan_sections(plan_resp.text)

        slide1_features = _parse_plan_lines(sections.get("SLIDE1", ""), 5)
        extra_features = _parse_plan_lines(sections.get("EXTRA_FEATURES", ""), 8)
        rich_features = _parse_plan_lines(sections.get("RICH_FEATURES", ""), 3)
        rich_tips = _parse_plan_lines(sections.get("RICH_TIPS", ""), 3)

        if not slide1_features:
            await progress.edit_text("🔴 Не удалось разобрать план по слайдам (сломанный формат ответа LLM) — попробуй ещё раз.")
            return

        # Слайдов ровно столько, сколько уникальных фото прислал пользователь
        # (правка 17.07.2026). Слайд 1 — фото[0] + топ-преимущества. Слайды
        # 2..N — по одному отдельному преимуществу на фото. Если EXTRA_FEATURES
        # не хватило на все доп. фото — добираем из RICH_FEATURES (не
        # выдумываем новое), убирая забранное из rich content, чтобы не
        # задублировать.
        n_extra_needed = len(photos) - 1
        while len(extra_features) < n_extra_needed and rich_features:
            extra_features.append(rich_features.pop(0))
        extra_features = extra_features[:n_extra_needed]

        template = pick_random_rarity_template()
        await progress.edit_text(
            f"Rarity: рендерю {len(photos)} слайд(ов) (фон: {template['key']}, шрифт: {font_family})..."
        )

        slide1 = await build_rarity_slide(template, product, slide1_features, photos[0])
        if slide1:
            await send_photo(message, slide1, f"Слайд 1: {product} (шрифт: {font_family})")
        else:
            await message.answer("🔴 Слайд 1 не получился.")

        for i, photo in enumerate(photos[1:], start=2):
            feature = extra_features[i - 2] if i - 2 < len(extra_features) else None
            char_pairs = [feature] if feature else []
            slide = await build_rarity_slide(template, product, char_pairs, photo)
            if slide:
                await send_photo(message, slide, f"Слайд {i}: {product}")
            else:
                await message.answer(f"🔴 Слайд {i} не получился.")

        await progress.edit_text("Rarity: генерирую rich content...")
        richcontent = await make_richcontent(
            product, rich_features, rich_tips, photos[0], extra_photos=photos[1:3], llm=llm,
            gaming_accent=None, category=category,
        )
        if richcontent:
            await send_photo(message, richcontent, f"Rich content: {product}")
        else:
            await message.answer("🔴 Rich content не получился.")

        await progress.edit_text("Rarity готов.")
    except Exception as e:
        log.error(f"rarity [{product_arg}]: {e}", exc_info=True)
        await progress.edit_text(f"🔴 Ошибка: {e}")
    finally:
        if font_token is not None:
            reset_rarity_family(font_token)
