import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import settings

router = Router()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIRM_WINDOW = 60  # секунд на подтверждение
_pending: dict[int, float] = {}


def _is_admin(user_id: int) -> bool:
    return user_id == settings.admin_id


@router.message(Command("self_destruct"))
async def cmd_self_destruct(message: Message):
    if not _is_admin(message.from_user.id):
        return  # не подтверждаем даже существование команды
    _pending[message.from_user.id] = time.monotonic()
    await message.answer(
        "⚠️ Это безвозвратно удалит ВСЮ папку "
        f"{PROJECT_ROOT} на этом компьютере — код, .env с ключами, базу данных.\n\n"
        f"Чтобы подтвердить, в течение {CONFIRM_WINDOW} секунд отправь:\n"
        "/self_destruct_confirm\n\n"
        "Если это случайность — просто ничего не делай, запрос сгорит сам."
    )


@router.message(Command("self_destruct_confirm"))
async def cmd_self_destruct_confirm(message: Message):
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return
    requested_at = _pending.pop(user_id, None)
    if requested_at is None or (time.monotonic() - requested_at) > CONFIRM_WINDOW:
        await message.answer(
            "Нет активного запроса на удаление (или он устарел). Сначала отправь /self_destruct."
        )
        return

    await message.answer("Подтверждено. Удаляю всё и отключаюсь — это последнее сообщение.")

    helper = _write_wipe_helper()
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(helper)],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )

    await asyncio.sleep(1)  # дать Telegram-запросу гарантированно уйти
    os._exit(0)  # процесс завершается сразу, ОС сама освобождает файлы (в т.ч. bot.db)


def _write_wipe_helper() -> Path:
    helper_path = Path(tempfile.gettempdir()) / "ai_bot_wipe.ps1"
    script = (
        "Start-Sleep -Seconds 3\n"
        f'Remove-Item -LiteralPath "{PROJECT_ROOT}" -Recurse -Force -ErrorAction SilentlyContinue\n'
        'Remove-Item -LiteralPath "$PSCommandPath" -Force -ErrorAction SilentlyContinue\n'
    )
    helper_path.write_text(script, encoding="utf-8")
    return helper_path
