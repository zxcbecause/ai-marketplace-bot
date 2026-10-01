import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database import db_connect
from services.llm import get_llm
from services.card import generate_full_card, detect_category, build_context_from_search
from utils.billing import save_cost
from utils.helpers import reply_long, strip_markdown
from prompts import DESCRIPTION_PROMPT

log = logging.getLogger(__name__)
router = Router()

_FIX_PROMPT = """{base_prompt}

ДОПОЛНИТЕЛЬНОЕ ТРЕБОВАНИЕ ОТ РЕДАКТОРА:
{fix_instruction}

Учти это требование при написании текста."""


async def _save_result(user_id: int, action: str, text: str):
    async with db_connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO last_result (user_id, action, text, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (user_id, action, text)
        )
        await db.commit()


async def _get_cache(user_id: int) -> dict | None:
    async with db_connect() as db:
        cursor = await db.execute(
            "SELECT product, context, category, last_action FROM card_cache WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _run_fix(message: Message, fix_instruction: str):
    user_id = message.from_user.id
    cache = await _get_cache(user_id)

    if not cache:
        await message.answer(
            "Нет сохранённой карточки.\n"
            "Сначала сделай /card Название товара."
        )
        return

    product  = cache["product"]
    context  = cache["context"]
    category = cache["category"]
    action   = cache.get("last_action", "desc")
    llm = await get_llm(user_id)

    progress = await message.answer(f"Переписываю: {product}...")

    try:
        if action in ("card", "desc"):
            fixed_prompt = _FIX_PROMPT.format(
                base_prompt=DESCRIPTION_PROMPT,
                fix_instruction=fix_instruction,
            )
            resp = await llm.chat(context, fixed_prompt)
            text = strip_markdown(resp.text.strip())
            await reply_long(progress, text)
            await save_cost(user_id, "fix", response=resp)
            await _save_result(user_id, "desc", text)

        elif action == "chars":
            from services.card import build_chars_prompt
            from prompts.characteristics import CHARACTERISTICS_FALLBACK
            chars_prompt, _ = build_chars_prompt(category)
            fix_chars_prompt = (
                chars_prompt + f"\n\nДОПОЛНИТЕЛЬНОЕ ТРЕБОВАНИЕ: {fix_instruction}"
            )
            resp = await llm.chat(context, fix_chars_prompt)
            text = strip_markdown(resp.text.strip())
            await reply_long(progress, text)
            await save_cost(user_id, "fix", response=resp)
            await _save_result(user_id, "chars", text)

        else:
            await progress.edit_text(
                f"Не знаю как переделать действие «{action}».\n"
                f"Поддерживается: /fix для описания и характеристик."
            )
            return

        await message.answer(f"{resp.provider}: ${resp.usd:.4f}")

    except asyncio.CancelledError:
        await progress.edit_text("Отменено.")
    except Exception as e:
        log.error(f"Fix failed: {e}", exc_info=True)
        await progress.edit_text(f"Ошибка: {e}")


async def _run_retry(message: Message):
    user_id = message.from_user.id
    cache = await _get_cache(user_id)

    if not cache:
        await message.answer(
            "Нет сохранённой карточки.\n"
            "Сначала сделай /card Название товара."
        )
        return

    product  = cache["product"]
    context  = cache["context"]
    category = cache["category"]
    action   = cache.get("last_action", "card")
    llm = await get_llm(user_id)

    progress = await message.answer(f"Повторяю {action}: {product}...")

    try:
        if action in ("card", "desc"):
            from services.card.generator import generate_description
            description, resp = await generate_description(context, llm)
            await reply_long(progress, strip_markdown(description))
            await save_cost(user_id, "retry", response=resp)
            await message.answer(f"{resp.provider}: ${resp.usd:.4f}")

        elif action == "chars":
            from services.card.generator import generate_characteristics
            chars_text, resp, cat_found = await generate_characteristics(context, category, llm)
            if not cat_found:
                await message.answer(f"Категория «{category}» не найдена — общий шаблон.")
            await reply_long(progress, strip_markdown(chars_text))
            await save_cost(user_id, "retry", response=resp)
            await message.answer(f"{resp.provider}: ${resp.usd:.4f}")

        elif action == "image":
            await progress.edit_text("Для повтора инфографики используй /image")

        else:
            await progress.edit_text(f"Неизвестное действие: {action}")

    except asyncio.CancelledError:
        await progress.edit_text("Отменено.")
    except Exception as e:
        log.error(f"Retry failed: {e}", exc_info=True)
        await progress.edit_text(f"Ошибка: {e}")


@router.message(Command("fix"))
async def cmd_fix(message: Message):
    text = message.text or ""
    parts = text.split(None, 1)
    fix_instruction = parts[1].strip() if len(parts) > 1 else ""

    if not fix_instruction:
        await message.answer(
            "Напиши что исправить:\n"
            "/fix сделай текст короче\n"
            "/fix убери упоминание цены\n"
            "/fix перепиши первый абзац в более живом стиле"
        )
        return

    await _run_fix(message, fix_instruction)


@router.message(Command("retry"))
async def cmd_retry(message: Message):
    await _run_retry(message)
