import json
import logging

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database import db_connect
from services.llm import get_llm
from services.search import find_product_images
from services.image import (
    make_infographic, make_gaming_infographic, make_speakers_infographic,
    make_mice_infographic, make_ram_infographic, make_gpu_infographic,
    make_watch_infographic, make_signal_infographic, make_chair_infographic, make_richcontent,
)
from utils.helpers import send_photo
from handlers.image import USE_VISION

log = logging.getLogger(__name__)
router = Router()


async def _get_batch_item(user_id: int, msg_id: int) -> dict | None:
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM batch_items
               WHERE user_id = ? AND (message_id = ? OR richcontent_msg_id = ?)
               ORDER BY id DESC LIMIT 1""",
            (user_id, msg_id, msg_id)
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _update_batch_item(item_id: int, message_id: int | None,
                              richcontent_msg_id: int | None, used_photo_urls: list[str]):
    async with db_connect() as db:
        await db.execute(
            """UPDATE batch_items
               SET message_id = ?, richcontent_msg_id = ?, used_photo_urls = ?
               WHERE id = ?""",
            (message_id, richcontent_msg_id, json.dumps(used_photo_urls, ensure_ascii=False), item_id)
        )
        await db.commit()


@router.message(Command("altphoto"))
async def cmd_altphoto(message: Message):
    if not message.reply_to_message:
        await message.answer(
            "Ответь командой /altphoto на сообщение с инфографикой или rich content "
            "(в том числе из прошлого /batch) — подберу альтернативное фото."
        )
        return

    user_id = message.from_user.id
    item = await _get_batch_item(user_id, message.reply_to_message.message_id)
    if not item:
        await message.answer(
            "Не нашёл данные по этой карточке — /altphoto работает только в ответ "
            "на инфографику или rich content, отправленные через /batch."
        )
        return

    product  = item["product"]
    brand    = item["brand"] or ""
    color    = item["color"] or ""
    color_en = item["color_en"] or ""
    category = item["category"] or ""
    slogan   = item["slogan"] or ""
    user_style = item["user_style"] or "default"
    features = [tuple(x) for x in json.loads(item["features"] or "[]")]
    tips     = [tuple(x) for x in json.loads(item["tips"] or "[]")]
    rich_slogan = item["rich_slogan"] or ""
    if rich_slogan:
        tips = tips + [(rich_slogan, "")]
    gaming_accent = tuple(json.loads(item["gaming_accent"])) if item["gaming_accent"] else None
    used_urls = json.loads(item["used_photo_urls"] or "[]")

    llm = await get_llm(user_id)
    progress = await message.answer(f"Ищу альтернативное фото для «{product}»...")

    photos = await find_product_images(
        product, n=1, brand=brand, color=color, color_en=color_en,
        category=category,
        llm=llm, vision_validate=USE_VISION, exclude_urls=used_urls,
    )
    if not photos:
        await progress.edit_text(
            f"❌ Альтернативное фото для «{product}»{f' ({color})' if color else ''} "
            f"не найдено — оставляю текущий вариант."
        )
        return

    img_bytes = photos[0]
    new_urls = getattr(find_product_images, "_last_urls", [])

    if category == "Мыши":
        infographic, bg_warning = await make_mice_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif category == "Оперативная память":
        infographic, bg_warning = await make_ram_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif category == "Видеокарты":
        infographic, bg_warning = await make_gpu_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif category == "Смарт-часы":
        infographic, bg_warning = await make_watch_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif category == "Сетевое оборудование":
        infographic, bg_warning = await make_signal_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif category == "Игровые кресла":
        infographic, bg_warning = await make_chair_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif user_style == "gaming":
        infographic, bg_warning = await make_gaming_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    elif category == "Акустика":
        infographic, bg_warning = await make_speakers_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=gaming_accent,
        )
    else:
        infographic, bg_warning = await make_infographic(
            product, features, img_bytes, llm=llm, brand=brand, slogan=slogan,
        )

    color_tag = f" ({color})" if color else ""
    new_msg_id = item["message_id"]
    if infographic:
        caption = f"Инфографика (альт. фото): {product}{color_tag}"
        if bg_warning:
            caption += f"\n⚠️ {bg_warning}. Белый фон."
        sent = await send_photo(message, infographic, caption)
        new_msg_id = sent.message_id

    new_rc_msg_id = item["richcontent_msg_id"]
    if item["richcontent_msg_id"]:
        richcontent = await make_richcontent(
            product, features, tips, img_bytes,
            extra_photos=[], llm=llm, gaming_accent=gaming_accent,
        )
        if richcontent:
            sent = await send_photo(message, richcontent, f"Rich content (альт. фото): {product}{color_tag}")
            new_rc_msg_id = sent.message_id

    await progress.delete()
    await _update_batch_item(item["id"], new_msg_id, new_rc_msg_id, used_urls + new_urls)
