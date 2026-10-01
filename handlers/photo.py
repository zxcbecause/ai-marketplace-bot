import asyncio
import io
import logging

from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message, PhotoSize

from database import db_connect
from services.llm import get_llm
from services.card import (
    detect_category, get_priority_chars, build_context_from_search,
    parse_product_info, clean_product_name,
)
from services.image import make_infographic, make_richcontent
from utils.helpers import send_photo
from utils.billing import save_cost
from prompts import KEY_CHARS_PROMPT, RICHCONTENT_TIPS_PROMPT, SLOGAN_PROMPT

log = logging.getLogger(__name__)
router = Router()

# Временное хранилище фото per user (bytes)
# Живёт до следующей операции с изображением
_pending_photos: dict[int, bytes] = {}


async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


def _parse_features(raw: str) -> list[tuple[str, str]]:
    features = []
    for line in raw.strip().splitlines():
        if "|" in line:
            t, v = line.split("|", 1)
            t, v = t.strip().upper(), v.strip()
            if t and v:
                features.append((t, v))
    return features[:4]


def _parse_tips(raw: str) -> list[tuple[str, str]]:
    tips = []
    for line in raw.strip().splitlines():
        if "|" in line:
            t, v = line.split("|", 1)
            tips.append((t.strip(), v.strip()))
    return tips[:3]


def _split_visual_sections(text: str) -> dict[str, str]:
    """Разбирает ответ объединённого промпта по маркерам [FEATURES]/[SLOGAN]/[TIPS]
    (перенесено из handlers/image.py::_extract_visuals, 03.08.2026 — тот же
    паттерн объединения, что там уже даёт ~60% экономии input-токенов и 3
    меньше сетевых раундов на карточку)."""
    import re as _re
    sections: dict[str, list[str]] = {}
    current = None
    marker_re = _re.compile(
        r'^\s*\**\[?\**(FEATURES|SLOGAN|TIPS)\**\]?\**\s*:?\s*(.*)$',
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


def _parse_slogan(raw: str) -> str:
    slogan = raw.strip().strip('"').strip("'").splitlines()[0] if raw.strip() else ""
    words = slogan.split()
    return " ".join(words[:6]) if len(words) > 6 else slogan


async def _generate_visuals(message: Message, product: str,
                             context: str, category: str, img_bytes: bytes,
                             brand: str = ""):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    priority = get_priority_chars(category)
    if priority:
        feat_prompt = (
            f"КАТЕГОРИЯ ТОВАРА: {category}\n"
            f"ПРИОРИТЕТ: {' → '.join(priority)}\n\n" + KEY_CHARS_PROMPT
        )
    else:
        feat_prompt = KEY_CHARS_PROMPT

    hint = f"[ЦЕЛЕВАЯ МОДЕЛЬ: {product}]\n\n"

    # Features + slogan + tips ОДНИМ вызовом (03.08.2026, перенесено из
    # handlers/image.py::_extract_visuals) вместо трёх последовательных —
    # тот же контекст раньше пересылался трижды. При сломанном формате
    # ответа — фолбэк на старые три отдельных вызова.
    combined = (
        "Ты готовишь тексты для инфографики карточки товара.\n"
        "Выполни ТРИ независимые задачи по правилам ниже. Фразы «выдай только…» "
        "внутри задач относятся к содержимому соответствующей секции ответа.\n\n"
        "═══ ЗАДАЧА 1 — секция [FEATURES] ═══\n" + feat_prompt +
        "\n\n═══ ЗАДАЧА 2 — секция [SLOGAN] ═══\n" + SLOGAN_PROMPT +
        "\n\n═══ ЗАДАЧА 3 — секция [TIPS] ═══\n" + RICHCONTENT_TIPS_PROMPT +
        "\n\n═══ ФОРМАТ ОТВЕТА (строго эти 3 маркера, каждый на своей строке) ═══\n"
        "[FEATURES]\n<строки ЗАГОЛОВОК|значение>\n"
        "[SLOGAN]\n<одна строка>\n"
        "[TIPS]\n<строки ЗАГОЛОВОК | текст>"
    )
    resp = await llm.chat(hint + context, combined)
    sections = _split_visual_sections(resp.text)
    features = _parse_features(sections.get("FEATURES", ""))
    slogan = _parse_slogan(sections.get("SLOGAN", ""))
    tips = _parse_tips(sections.get("TIPS", ""))
    vis_resps = [resp]

    if not (features and slogan and tips):
        log.warning(
            f"_generate_visuals: формат сломан (f={len(features)} s={bool(slogan)} "
            f"t={len(tips)}) — фолбэк на раздельные вызовы"
        )
        feat_resp = await llm.chat(hint + context, feat_prompt)
        features = _parse_features(feat_resp.text)

        tips_resp = await llm.chat(hint + context, RICHCONTENT_TIPS_PROMPT)
        tips = _parse_tips(tips_resp.text)

        slogan_resp = await llm.chat(hint + context, SLOGAN_PROMPT)
        slogan = _parse_slogan(slogan_resp.text)

        vis_resps = [resp, feat_resp, tips_resp, slogan_resp]

    for _vr in vis_resps:
        await save_cost(user_id, "photo_visuals", response=_vr)

    # Инфографика
    infographic, _ = await make_infographic(
        product, features, img_bytes, llm=llm, brand=brand, slogan=slogan,
    )
    if infographic:
        await send_photo(message, infographic, f"Инфографика: {product}")

    # Rich content
    richcontent = await make_richcontent(product, features, tips, img_bytes, llm=llm)
    if richcontent:
        await send_photo(message, richcontent, f"Rich content: {product}")


@router.message(Command("watermark"))
async def cmd_watermark(message: Message, bot: Bot):
    """Накладывает водяной знак магазина на присланное фото без какой-либо генерации."""
    if not message.photo:
        await message.answer("Прикрепи фото с подписью /watermark.")
        return

    best_photo = max(message.photo, key=lambda p: p.file_size or 0)
    progress = await message.answer("Накладываю водяной знак...")
    try:
        from PIL import Image
        from services.image.logo import paste_brand_watermark, detect_bg_dark

        img_bytes = await _download_photo(bot, best_photo)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        bg_dark = detect_bg_dark(img, corner="bottom-left", scale=0.25)
        result = paste_brand_watermark(img, corner="bottom-left", scale=0.25, opacity=0.55,
                                        bg_dark=bg_dark, normalize_alpha=True)

        out = io.BytesIO()
        result.save(out, format="JPEG", quality=92)
        await send_photo(message, out.getvalue(), "")
    except Exception as e:
        log.error(f"watermark failed: {e}", exc_info=True)
        await message.answer(f"Ошибка: {e}")
    await progress.delete()


@router.message(lambda m: m.photo is not None)
async def handle_photo(message: Message, bot: Bot):
    user_id = message.from_user.id

    # Скачиваем фото (берём наибольший размер)
    best_photo = max(message.photo, key=lambda p: p.file_size or 0)
    progress = await message.answer("Загружаю фото...")

    try:
        img_bytes = await _download_photo(bot, best_photo)
    except Exception as e:
        await progress.edit_text(f"Не удалось загрузить фото: {e}")
        return

    # Есть ли подпись к фото? — используем как название товара
    caption = (message.caption or "").strip()

    if caption:
        # Пользователь указал товар в подписи
        llm = await get_llm(user_id)
        info = await parse_product_info(clean_product_name(caption), llm)
        product = info.full_name
        category = detect_category(product)
        await progress.edit_text(f"Ищу информацию: {product}...")
        context, exa_count, search_resps = await build_context_from_search(
            product, category, llm
        )
        for r in search_resps:
            await save_cost(user_id, "photo_search", response=r)

        # Сохраняем в кэш
        async with db_connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO card_cache
                   (user_id, product, brand, model, context, category, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                (user_id, product, info.brand, info.model, context, category)
            )
            await db.commit()

        await progress.edit_text("Генерирую инфографику и rich content...")
        try:
            await _generate_visuals(
                message, product, context, category, img_bytes,
                brand=info.brand,
            )
        except Exception as e:
            log.error(f"Photo visuals failed: {e}", exc_info=True)
            await message.answer(f"Ошибка генерации: {e}")
        await progress.delete()

    else:
        # Подписи нет — проверяем кэш
        async with db_connect() as db:
            cursor = await db.execute(
                "SELECT product, context, category, brand FROM card_cache WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()

        if row:
            product  = row[0]
            context  = row[1]
            category = row[2]
            brand    = row[3] or ""
            await progress.edit_text(
                f"Использую фото для: {product}\n"
                f"Генерирую инфографику и rich content..."
            )
            try:
                await _generate_visuals(
                    message, product, context, category, img_bytes,
                    brand=brand,
                )
            except Exception as e:
                log.error(f"Photo visuals failed: {e}", exc_info=True)
                await message.answer(f"Ошибка генерации: {e}")
            await progress.delete()
        else:
            # Нет ни подписи ни кэша — сохраняем фото и просим название
            _pending_photos[user_id] = img_bytes
            await progress.edit_text(
                "Фото сохранено.\n\n"
                "Отправь название товара в подписи к фото, или сначала сделай /card, "
                "потом отправь фото."
            )


@router.message(Command("usephoto"))
async def cmd_usephoto(message: Message):
    """Использовать сохранённое фото с текущей карточкой."""
    user_id = message.from_user.id
    img_bytes = _pending_photos.get(user_id)
    if not img_bytes:
        await message.answer("Нет сохранённого фото. Сначала отправь фото товара.")
        return

    async with db_connect() as db:
        cursor = await db.execute(
            "SELECT product, context, category, brand FROM card_cache WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()

    if not row:
        await message.answer(
            "Нет сохранённой карточки. Сначала сделай /card Название."
        )
        return

    product  = row["product"]
    context  = row["context"]
    category = row["category"]
    brand    = row["brand"] or ""
    _pending_photos.pop(user_id, None)

    progress = await message.answer(f"Генерирую с твоим фото: {product}...")
    try:
        await _generate_visuals(
            message, product, context, category, img_bytes,
            brand=brand,
        )
    except Exception as e:
        log.error(f"usephoto failed: {e}", exc_info=True)
        await message.answer(f"Ошибка: {e}")
    await progress.delete()
