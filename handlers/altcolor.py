import json
import logging

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database import db_connect
from services.llm import get_llm
from services.image import (
    make_gaming_infographic, make_speakers_infographic,
    make_mice_infographic, make_ram_infographic, make_gpu_infographic,
    make_watch_infographic, make_signal_infographic, make_chair_infographic,
    make_richcontent, pick_alt_accent,
)
from utils.helpers import send_photo

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
                              richcontent_msg_id: int | None, accent: tuple[int, int, int]):
    async with db_connect() as db:
        await db.execute(
            """UPDATE batch_items
               SET message_id = ?, richcontent_msg_id = ?, gaming_accent = ?
               WHERE id = ?""",
            (message_id, richcontent_msg_id, json.dumps(list(accent)), item_id)
        )
        await db.commit()


@router.message(Command("altcolor"))
async def cmd_altcolor(message: Message):
    if not message.reply_to_message:
        await message.answer(
            "Ответь командой /altcolor на сообщение с инфографикой или rich content "
            "(в том числе из прошлого /batch) — перекрашу в другой акцентный цвет."
        )
        return

    user_id = message.from_user.id
    item = await _get_batch_item(user_id, message.reply_to_message.message_id)
    if not item:
        await message.answer(
            "Не нашёл данные по этой карточке — /altcolor работает только в ответ "
            "на инфографику или rich content, отправленные через /batch."
        )
        return

    if not item["gaming_accent"]:
        await message.answer(
            "Эта карточка не использует акцентный цвет (обычный стиль фона, не gaming/виджет) "
            "— /altcolor тут не применим."
        )
        return

    product  = item["product"]
    brand    = item["brand"] or ""
    category = item["category"] or ""
    features = [tuple(x) for x in json.loads(item["features"] or "[]")]
    tips     = [tuple(x) for x in json.loads(item["tips"] or "[]")]
    rich_slogan = item["rich_slogan"] or ""
    if rich_slogan:
        tips = tips + [(rich_slogan, "")]
    old_accent = tuple(json.loads(item["gaming_accent"]))

    llm = await get_llm(user_id)
    new_accent = pick_alt_accent(category, exclude=old_accent)
    log.info(f"/altcolor: {product!r} {old_accent} -> {new_accent}")

    # Фото у нас тут нет (берём из самого сообщения нельзя — Telegram не
    # отдаёт исходные байты чужого сообщения без скачивания по file_id,
    # а file_id мы не сохраняем) — поэтому /altcolor требует, чтобы
    # инфографика/rich content всё ещё были доступны для перерисовки по
    # кэшу. Тут переиспользуем тот же путь, что /altphoto — фото не нужно
    # заново искать, достаточно URL последнего использованного.
    used_urls = json.loads(item["used_photo_urls"] or "[]")
    if not used_urls:
        await message.answer("⚠️ Не сохранилось фото для этой карточки — /altcolor не сможет перерисовать.")
        return

    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(used_urls[-1], ssl=False, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                await message.answer("⚠️ Не удалось скачать фото товара заново — /altcolor не сработал.")
                return
            img_bytes = await r.read()

    if category == "Мыши":
        infographic, bg_warning = await make_mice_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    elif category == "Оперативная память":
        infographic, bg_warning = await make_ram_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    elif category == "Видеокарты":
        infographic, bg_warning = await make_gpu_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    elif category == "Смарт-часы":
        infographic, bg_warning = await make_watch_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    elif category == "Акустика":
        infographic, bg_warning = await make_speakers_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    elif category == "Сетевое оборудование":
        infographic, bg_warning = await make_signal_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    elif category == "Игровые кресла":
        infographic, bg_warning = await make_chair_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )
    else:
        infographic, bg_warning = await make_gaming_infographic(
            product, features, img_bytes, llm=llm,
            brand=brand, category=category, accent=new_accent,
        )

    new_msg_id = item["message_id"]
    if infographic:
        caption = f"Инфографика (другой цвет): {product}"
        if bg_warning:
            caption += f"\n⚠️ {bg_warning}"
        sent = await send_photo(message, infographic, caption)
        new_msg_id = sent.message_id

    new_rc_msg_id = item["richcontent_msg_id"]
    if item["richcontent_msg_id"]:
        richcontent = await make_richcontent(
            product, features, tips, img_bytes,
            extra_photos=[], llm=llm, gaming_accent=new_accent,
        )
        if richcontent:
            sent = await send_photo(message, richcontent, f"Rich content (другой цвет): {product}")
            new_rc_msg_id = sent.message_id

    await _update_batch_item(item["id"], new_msg_id, new_rc_msg_id, new_accent)
