import re
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.types import BufferedInputFile


def strip_markdown(text: str) -> str:
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^-{2,}\s*$', '', text, flags=re.MULTILINE)
    return text.strip()


async def reply_long(message: Message, text: str, chunk_size: int = 4000):
    """Отправляет длинный текст частями."""
    text = text.strip()
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        try:
            await message.answer(text[i:i + chunk_size])
        except TelegramBadRequest:
            # Текст может содержать «<1 мс» и т.п. — дефолтный parse_mode=HTML
            # ломается на «<цифра» как на теге. Повторяем без разметки.
            await message.answer(text[i:i + chunk_size], parse_mode=None)


async def send_photo(message: Message, data: bytes, caption: str = "") -> Message:
    """Отправляет фото из bytes — совместимо с aiogram 3."""
    photo = BufferedInputFile(data, filename="image.jpg")
    try:
        return await message.answer_photo(photo=photo, caption=caption)
    except TelegramBadRequest:
        # Подпись может содержать «<1 мс» — parse_mode=HTML ломается. Без разметки.
        return await message.answer_photo(photo=photo, caption=caption, parse_mode=None)
