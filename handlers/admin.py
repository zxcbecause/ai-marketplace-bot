import asyncio
import os
import subprocess

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from utils.runtime_env import IS_WINDOWS, disk_root
from database import CostsRepository, UsersRepository, db_connect
from services.llm import set_llm

router = Router()

ALLOWED_MODELS = {"deepseek", "openai", "gemini"}


def is_admin(user_id: int) -> bool:
    return user_id == settings.admin_id


@router.message(Command("model"))
async def cmd_model(message: Message):
    args = message.text.split()[1:]
    if not args:
        async with db_connect() as db:
            current = await UsersRepository(db).get_llm_model(message.from_user.id)
        await message.answer(
            f"Текущая модель: <b>{current}</b>\n\n"
            f"Доступные: {', '.join(ALLOWED_MODELS)}\n"
            f"Использование: /model deepseek"
        )
        return

    model = args[0].lower()
    if model not in ALLOWED_MODELS:
        await message.answer(f"Неизвестная модель. Доступные: {', '.join(ALLOWED_MODELS)}")
        return

    await set_llm(message.from_user.id, model)
    await message.answer(f"Модель переключена на <b>{model}</b>")


@router.message(Command("adduser"))
async def cmd_adduser(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        await message.answer("Использование: /adduser 123456789")
        return

    new_id = int(args[0])
    async with db_connect() as db:
        await UsersRepository(db).add(new_id)
    await message.answer(f"Пользователь {new_id} добавлен.")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        await message.answer("Использование: /removeuser 123456789")
        return

    rm_id = int(args[0])
    async with db_connect() as db:
        await UsersRepository(db).remove(rm_id)
    await message.answer(f"Пользователь {rm_id} удалён.")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    async with db_connect() as db:
        rows = await UsersRepository(db).list_all()

    if not rows:
        await message.answer("Список пользователей пуст.")
        return

    lines = [f"<b>Пользователи ({len(rows)}):</b>"]
    for row in rows:
        marker = " 👑" if row[0] == settings.admin_id else ""
        lines.append(f"• {row[0]} [{row[1]}]{marker}")
    await message.answer("\n".join(lines))


@router.message(Command("health"))
async def cmd_health(message: Message):
    """Проверка систем: баланс DeepSeek, Ozon/WB API, диск."""
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    progress = await message.answer("Проверяю системы...")
    import asyncio
    import httpx
    import shutil

    lines = ["<b>Состояние систем:</b>"]

    # DeepSeek баланс
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as c:
            r = await c.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            )
        data = r.json()
        bal = float((data.get("balance_infos") or [{}])[0].get("total_balance", 0) or 0)
        ok = data.get("is_available", False)
        icon = "✅" if ok and bal >= 1 else ("⚠️" if ok else "❌")
        lines.append(f"{icon} DeepSeek: ${bal:.2f}" + ("" if ok else " (НЕДОСТУПЕН)"))
    except Exception as e:
        lines.append(f"❌ DeepSeek: ошибка ({str(e)[:50]})")

    # Ozon Seller API
    try:
        from services.ozon.client import _post
        data = await asyncio.to_thread(
            _post, "/v3/product/list", {"limit": 1, "last_id": "", "filter": {}}
        )
        total = data.get("result", {}).get("total", "?")
        lines.append(f"✅ Ozon API: работает ({total} товаров в кабинете)")
    except Exception as e:
        lines.append(f"❌ Ozon API: {str(e)[:60]}")

    # WB Content API
    try:
        import requests as _rq
        r = await asyncio.to_thread(lambda: _rq.get(
            "https://content-api.wildberries.ru/ping",
            headers={"Authorization": settings.wb_api_key}, timeout=15,
        ))
        lines.append("✅ WB API: работает" if r.status_code == 200
                     else f"⚠️ WB API: HTTP {r.status_code}")
    except Exception as e:
        lines.append(f"❌ WB API: {str(e)[:60]}")

    # Диск
    try:
        root = disk_root(settings.db_path)
        du = shutil.disk_usage(root)
        free_gb = du.free / 1024**3
        icon = "✅" if free_gb > 20 else "⚠️"
        lines.append(f"{icon} Диск {root}: свободно {free_gb:.0f} ГБ")
    except Exception:
        pass

    await progress.edit_text("\n".join(lines))


@router.message(Command("costs"))
async def cmd_costs(message: Message):
    """Сводка трат: сегодня, вчера, 7 дней, 30 дней, всего — по провайдерам."""
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    async with db_connect() as db:
        repo = CostsRepository(db)
        summary = await repo.summary()
        top = await repo.top_operations(days=7, limit=5)

    lines = ["<b>Траты (LLM):</b>"]
    for label, rows in summary:
        if not rows:
            lines.append(f"\n<b>{label}:</b> —")
            continue
        total = sum(r.usd for r in rows)
        parts = ", ".join(f"{r.provider}: ${r.usd:.2f} ({r.calls})" for r in rows)
        lines.append(f"\n<b>{label}:</b> ${total:.2f}\n  {parts}")
    if top:
        lines.append("\n<b>Топ операций (7 дней):</b>")
        for op in top:
            lines.append(f"  {op.operation}: ${op.usd:.2f} ({op.calls})")
    await message.answer("\n".join(lines))


@router.message(Command("shutdown"))
async def cmd_shutdown(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    if not IS_WINDOWS:
        await message.answer("Команда работает только на Windows-ПК, не в контейнере.")
        return
    subprocess.run(["shutdown", "/s", "/f", "/t", "30"])
    await message.answer(
        "ПК выключится через 30 секунд.\nОтменить: /cancelshutdown"
    )


@router.message(Command("cancelshutdown"))
async def cmd_cancelshutdown(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    if not IS_WINDOWS:
        await message.answer("Команда работает только на Windows-ПК, не в контейнере.")
        return
    subprocess.run(["shutdown", "/a"])
    await message.answer("Выключение отменено.")


@router.message(Command("restart_bot"))
async def cmd_restart_bot(message: Message):
    """Перезапуск процесса бота (не всего ПК) — через start.ps1, тот же
    путь, что при ручном рестарте: архивирует текущий bot_stderr.log,
    проверяет/поднимает SearXNG, запускает свежий main.py detached. Текущий
    процесс завершается сам сразу после — окно двойного polling'а короче
    времени прогрева моделей нового процесса (~10с), конфликта не будет."""
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("Перезапускаюсь — вернусь секунд через 15-20.")
    if not IS_WINDOWS:
        # В Docker перезапуск делает сам контейнер (restart: unless-stopped).
        await asyncio.sleep(0.5)
        os._exit(0)
    subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    await asyncio.sleep(0.5)
    os._exit(0)
