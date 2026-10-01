import asyncio
import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database import db_connect
from services.llm import get_llm
from services.card import generate_full_card, CardResult
from utils.billing import save_cost
from utils.helpers import reply_long, strip_markdown
from handlers.tasks import run_task, cancel_task

log = logging.getLogger(__name__)
router = Router()


async def _save_to_cache(user_id: int, result: CardResult, action: str = "card"):
    async with db_connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO card_cache
               (user_id, product, brand, model, color, color_en, context, category, last_action, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (user_id, result.product, result.brand, result.model,
             result.color, result.color_en, result.context, result.category, action),
        )
        await db.commit()


async def _run_card(message: Message, product: str, url: str | None):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    progress = await message.answer(f"Ищу информацию: {product}...")

    try:
        result = await generate_full_card(product, llm, url=url)
    except asyncio.CancelledError:
        await progress.edit_text("Отменено.")
        return
    except Exception as e:
        log.error(f"Card generation failed for {product}: {e}", exc_info=True)
        await progress.edit_text(f"Ошибка при генерации: {e}")
        return

    action = "desc" if result.characteristics == "" else "card"
    await _save_to_cache(user_id, result, action)

    # Описание
    await reply_long(progress, strip_markdown(result.description))

    # Характеристики
    if result.characteristics:
        await reply_long(message, strip_markdown(result.characteristics))

    # Упаковка
    if result.packaging:
        await message.answer(result.packaging)

    # Стоимость
    summary = result.cost_summary()
    if summary:
        await message.answer(summary)

    # Биллинг в БД (exa_requests — только с первым ответом, иначе задваивается)
    for i, resp in enumerate(result.llm_responses):
        await save_cost(user_id, "card", response=resp,
                        exa_requests=result.exa_requests if i == 0 else 0)


async def _run_desc(message: Message, product: str, raw_data: str):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    progress = await message.answer(f"Пишу описание: {product}...")

    try:
        from services.card.category import detect_category, build_chars_prompt
        from services.card.generator import _build_context_header, generate_description, build_context_from_search

        if raw_data:
            category = detect_category(product)
            context = _build_context_header(product, category) + f"Данные пользователя:\n{raw_data}"
        else:
            from services.card.normalize import parse_product_info
            raw_input = product
            info = await parse_product_info(product, llm)
            product = info.full_name
            category = detect_category(product) or detect_category(raw_input)
            context, exa_count, search_resps = await build_context_from_search(
                info.search_name, category, llm, raw_specs=raw_input
            )
            for r in search_resps:
                await save_cost(user_id, "desc_search", response=r)

        description, resp = await generate_description(context, llm)

    except asyncio.CancelledError:
        await progress.edit_text("Отменено.")
        return
    except Exception as e:
        log.error(f"Desc failed for {product}: {e}", exc_info=True)
        await progress.edit_text(f"Ошибка: {e}")
        return

    await reply_long(progress, strip_markdown(description))
    await save_cost(user_id, "desc", response=resp)
    await message.answer(f"{resp.provider}: ${resp.usd:.4f} ({resp.input_tokens}+{resp.output_tokens} тк)")


async def _run_chars(message: Message, product: str, raw_data: str):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    progress = await message.answer(f"Собираю характеристики: {product}...")

    try:
        from services.card.category import detect_category
        category = detect_category(product)

        if raw_data:
            from services.card.generator import _build_context_header
            context = _build_context_header(product, category) + f"Данные пользователя:\n{raw_data}"
        else:
            from services.card.generator import build_context_from_search
            from services.card.normalize import parse_product_info
            raw_input = product
            info = await parse_product_info(product, llm)
            product = info.full_name
            category = detect_category(product) or category
            context, exa_count, search_resps = await build_context_from_search(
                info.search_name, category, llm, raw_specs=raw_input
            )
            for r in search_resps:
                await save_cost(user_id, "chars_search", response=r)

        from services.card.generator import generate_characteristics
        chars_text, resp, cat_found = await generate_characteristics(context, category, llm)

        if not cat_found:
            await message.answer(
                f"Категория «{category or 'не определена'}» не найдена в базе — использую общий шаблон."
            )

    except asyncio.CancelledError:
        await progress.edit_text("Отменено.")
        return
    except Exception as e:
        log.error(f"Chars failed for {product}: {e}", exc_info=True)
        await progress.edit_text(f"Ошибка: {e}")
        return

    await reply_long(progress, strip_markdown(chars_text))
    await save_cost(user_id, "chars", response=resp)
    await message.answer(f"{resp.provider}: ${resp.usd:.4f} ({resp.input_tokens}+{resp.output_tokens} тк)")


def _parse_args(message: Message) -> tuple[str, str | None, str]:
    """Парсит: /card Название [URL]\n[raw data]"""
    text = message.text or ""
    lines = text.strip().split("\n")
    first_line = lines[0].strip()
    raw_data = "\n".join(lines[1:]).strip()

    parts = first_line.split(None, 1)
    if len(parts) < 2:
        return "", None, ""

    args = parts[1].strip()
    url = None

    tokens = args.split()
    if tokens and tokens[-1].startswith("http"):
        url = tokens[-1]
        args = " ".join(tokens[:-1]).strip()

    return args, url, raw_data




# ── Хендлеры ──────────────────────────────────────────────────────────────────

@router.message(Command("card"))
async def cmd_card(message: Message):
    product, url, _ = _parse_args(message)
    if not product:
        await message.answer(
            "Использование:\n"
            "/card Название товара\n"
            "/card Название товара https://..."
        )
        return
    run_task(message.from_user.id, _run_card(message, product, url), message=message)


@router.message(Command("desc"))
async def cmd_desc(message: Message):
    text = message.text or ""
    lines = text.strip().split("\n")
    parts = lines[0].split(None, 1)
    if len(parts) < 2:
        await message.answer("Использование: /desc Название товара")
        return
    product = parts[1].strip()
    raw_data = "\n".join(lines[1:]).strip()
    run_task(message.from_user.id, _run_desc(message, product, raw_data), message=message)


@router.message(Command("chars"))
async def cmd_chars(message: Message):
    text = message.text or ""
    lines = text.strip().split("\n")
    parts = lines[0].split(None, 1)
    if len(parts) < 2:
        await message.answer("Использование: /chars Название товара")
        return
    product = parts[1].strip()
    raw_data = "\n".join(lines[1:]).strip()
    run_task(message.from_user.id, _run_chars(message, product, raw_data), message=message)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if cancel_task(message.from_user.id):
        await message.answer("Операция отменена.")
    else:
        await message.answer("Нет активных операций.")
