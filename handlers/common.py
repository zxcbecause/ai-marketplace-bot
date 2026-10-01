from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database import db_connect
from config import settings

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    async with db_connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (message.from_user.id,)
        )
        await db.commit()

    await message.answer(
        "Привет! Я бот-помощник для заполнения карточек товаров на WB и Ozon.\n\n"
        "/card Название товара — полная карточка\n"
        "/batch — пакетная обработка\n"
        "/image — инфографика и rich content\n"
        "/desc — только описание\n"
        "/chars — только характеристики\n"
        "/model — сменить модель LLM\n"
        "/myid — узнать свой ID"
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой user ID: <code>{message.from_user.id}</code>")


@router.message(Command("testgoodbye"))
async def cmd_testgoodbye(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    from main import _send_hello_goodbye
    await _send_hello_goodbye(message.bot)
