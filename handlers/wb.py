"""
Команда /wb_batch — батч-создание WB-карточек с динамическим шаблоном.

Формат:
/wb_batch Кабели
АРТИКУЛ1  Название товара 1
АРТИКУЛ2  Название товара 2

Бот запрашивает характеристики категории через WB Content API,
для каждого товара генерирует описание + характеристики через LLM,
и возвращает готовый Excel-файл в формате WB-шаблона.
"""
import asyncio
from pathlib import Path
import html
import io
import logging
import re

import openpyxl
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

from services.llm import get_llm
from services.wb_batch import fetch_subject_chars, process_wb_batch
from handlers.tasks import run_task

log = logging.getLogger(__name__)
router = Router()

_HELP = (
    "Формат:\n"
    "/wb_batch <Категория WB>\n"
    "АРТИКУЛ1  Название товара 1\n"
    "АРТИКУЛ2  Название товара 2\n\n"
    "Примеры категорий: Кабели, Зарядные устройства, Наушники, Мыши, Внешние аккумуляторы\n\n"
    "Артикул и название разделяй табуляцией или пробелом.\n"
    "/cancel — остановить"
)


def _split_line(raw: str) -> tuple[str, str]:
    # Приоритет 1: таб (копипаст из Excel)
    if "\t" in raw:
        art, _, name = raw.partition("\t")
        return art.strip(), name.strip()
    # Приоритет 2: два и более пробела подряд (артикул сам по себе содержит пробелы)
    import re as _re
    parts = _re.split(r" {2,}", raw, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    # Фолбэк: весь текст = название, артикул пустой
    return "", raw.strip()


async def _run_wb_batch(message: Message, subject: str, lines: list[tuple[str, str]]):
    user_id = message.from_user.id
    llm = await get_llm(user_id)
    total = len(lines)

    progress = await message.answer(f"WB Batch «{subject}»: ищу шаблон...")

    try:
        chars, canonical, tpl_path = await fetch_subject_chars(subject)
    except ValueError as e:
        await progress.edit_text(f"🔴 {e}")
        return
    except Exception as e:
        await progress.edit_text(f"🔴 Ошибка WB API: {e}")
        return

    source = f"локальный шаблон" if tpl_path else f"WB API ({sum(1 for c in chars if c.get('required'))} обяз.)"
    await progress.edit_text(
        f"WB Batch «{canonical}»: {total} товаров, {len(chars)} полей [{source}]. Начинаю..."
    )
    if not tpl_path:
        await message.answer(
            f"💡 Локального шаблона для «{canonical}» нет — работаю через WB API "
            f"напрямую. Если хочешь свой шаблон с проверкой/валидацией — добавь "
            f"файл «{canonical}.xlsx» в data/templates/wb."
        )

    try:
        excel_bytes = await process_wb_batch(
            subject=canonical,
            chars=chars,
            lines=lines,
            llm=llm,
            user_id=user_id,
            send_text=message.answer,
            template_path=tpl_path,
        )
    except Exception as e:
        log.error(f"wb_batch failed: {e}", exc_info=True)
        await message.answer(f"🔴 Ошибка: {e}")
        return

    filename = f"WB_{canonical}_{total}шт.xlsx".replace(" ", "_")
    doc = BufferedInputFile(excel_bytes, filename=filename)
    caption = f"WB Batch готов: «{canonical}», {total} товаров."
    if tpl_path:
        caption += f"\n📋 Использован локальный шаблон ({tpl_path.name})"
    await message.answer_document(doc, caption=caption)


def _parse_text_lines(data: bytes) -> list[tuple[str, str]]:
    """Читает текстовый файл (TSV/CSV) с артикулом и названием.
    Пробует кодировки UTF-8, CP1251, Latin-1."""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except Exception:
            continue
    else:
        text = data.decode("latin-1")

    lines = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        # Сначала пробуем таб
        if "\t" in raw_line:
            parts = raw_line.split("\t", 1)
        else:
            # Два и более пробела
            parts = re.split(r" {2,}", raw_line, maxsplit=1)
        if len(parts) == 2:
            art, name = parts[0].strip(), parts[1].strip()
            if art and name:
                # Пропускаем строку-заголовок
                if re.search(r'артикул', art.lower()):
                    continue
                lines.append((art, name))
    return lines


def _read_excel_lines(data: bytes) -> list[tuple[str, str]]:
    """Читает данные из файла. Поддерживает:
    - Настоящий xlsx (ZIP-архив) — через openpyxl
    - Текстовый TSV/CSV с расширением .xlsx — через текстовый парсер"""

    # Проверяем сигнатуру: PK = ZIP = настоящий xlsx
    is_zip = len(data) >= 2 and data[0] == 0x50 and data[1] == 0x4B
    if not is_zip:
        log.info(f"Файл не ZIP ({data[:4].hex()}) — читаем как текст/TSV")
        return _parse_text_lines(data)

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Ищем строку-заголовок
    art_col = name_col = None
    data_start = 0
    for row_idx, row in enumerate(rows[:5]):
        cells = [str(c or "").strip().lower() for c in row]
        for col_idx, cell in enumerate(cells):
            if re.search(r'артикул', cell):
                art_col = col_idx
            if re.search(r'назван|наименован|описан', cell):
                name_col = col_idx
        if art_col is not None and name_col is not None:
            data_start = row_idx + 1
            break

    if art_col is None:
        art_col = 0
    if name_col is None:
        name_col = 1

    lines = []
    for row in rows[data_start:]:
        if not row or len(row) <= max(art_col, name_col):
            continue
        art  = str(row[art_col]  or "").strip()
        name = str(row[name_col] or "").strip()
        if art and name:
            lines.append((art, name))
    return lines


async def _handle_wb_excel(message: Message, subject: str, file_id: str):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    progress = await message.answer(f"WB Batch «{subject}»: читаю файл...")

    # Скачиваем файл
    try:
        bot = message.bot
        buf = await bot.download(file_id)   # aiogram 3: возвращает BytesIO
        data = buf.getvalue()
        log.info(f"Файл скачан: {len(data)} байт")
    except Exception as e:
        await progress.edit_text(f"🔴 Не удалось скачать файл: {e}")
        return

    try:
        lines = _read_excel_lines(data)
    except Exception as e:
        await progress.edit_text(
            f"🔴 Не удалось прочитать файл: {e}\n\n"
            "Убедись что файл сохранён в формате .xlsx (Excel 2007+), не .xls или .csv."
        )
        return

    if not lines:
        await progress.edit_text(
            "🔴 Не нашёл строк с данными. Убедись что в файле есть колонки «Артикул» и «Название» (или просто две колонки)."
        )
        return

    await progress.edit_text(
        f"WB Batch «{subject}»: {len(lines)} строк прочитано. Ищу шаблон..."
    )

    try:
        chars, canonical, tpl_path = await fetch_subject_chars(subject)
    except ValueError as e:
        await progress.edit_text(f"🔴 {e}")
        return
    except Exception as e:
        await progress.edit_text(f"🔴 Ошибка WB API: {e}")
        return

    source = "локальный шаблон" if tpl_path else f"WB API"
    await progress.edit_text(
        f"WB Batch «{canonical}»: {len(lines)} товаров, {len(chars)} полей [{source}]. Начинаю..."
    )
    if not tpl_path:
        await message.answer(
            f"💡 Локального шаблона для «{canonical}» нет — работаю через WB API "
            f"напрямую. Если хочешь свой шаблон с проверкой/валидацией — добавь "
            f"файл «{canonical}.xlsx» в data/templates/wb."
        )

    try:
        excel_bytes = await process_wb_batch(
            subject=canonical,
            chars=chars,
            lines=lines,
            llm=llm,
            user_id=user_id,
            send_text=message.answer,
            template_path=tpl_path,
        )
    except Exception as e:
        log.error(f"wb_batch excel failed: {e}", exc_info=True)
        await message.answer(f"🔴 Ошибка: {e}")
        return

    filename = f"WB_{canonical}_{len(lines)}шт.xlsx".replace(" ", "_")
    doc = BufferedInputFile(excel_bytes, filename=filename)
    caption = f"WB Batch готов: «{canonical}», {len(lines)} товаров."
    if tpl_path:
        caption += f"\n📋 Использован локальный шаблон ({tpl_path.name})"
    await message.answer_document(doc, caption=caption)


async def _run_wb_create_excel(message: Message, file_id: str):
    """/wb_create по загруженному xlsx: колонки «Артикул»/«Название» (или
    первые две колонки) — категория определяется автоматически на каждую
    строку отдельно, как в текстовом /wb_create. Не путать с /wb_batch —
    там ОДНА категория на весь файл, тут у каждой строки своя."""
    progress = await message.answer("WB Create (файл): читаю...")
    try:
        bot = message.bot
        buf = await bot.download(file_id)
        data = buf.getvalue()
        log.info(f"Файл скачан: {len(data)} байт")
    except Exception as e:
        await progress.edit_text(f"🔴 Не удалось скачать файл: {e}")
        return

    try:
        items = _read_excel_lines(data)
    except Exception as e:
        await progress.edit_text(
            f"🔴 Не удалось прочитать файл: {e}\n\n"
            "Убедись что файл сохранён в формате .xlsx (Excel 2007+), не .xls или .csv."
        )
        return

    if not items:
        await progress.edit_text(
            "🔴 Не нашёл строк с данными. Нужны колонки «Артикул» и «Название» "
            "(или просто две колонки без заголовка — первая артикул, вторая название)."
        )
        return

    await progress.edit_text(f"WB Create (файл): {len(items)} строк прочитано. Начинаю...")
    await _run_wb_create(message, items)


@router.message(lambda m: m.document is not None)
async def handle_document(message: Message):
    """Получает xlsx-файл. Подпись = категория WB → /wb_batch (один шаблон
    на все строки). Подпись пустая → /wb_create (категория своя на каждую
    строку, определяется автоматически, как при текстовом вводе)."""
    doc = message.document
    fname = (doc.file_name or "").lower()
    log.info(f"Document received: {doc.file_name!r} mime={doc.mime_type!r}")

    if not fname.endswith(".xlsx") and not fname.endswith(".xls"):
        return  # не Excel — пропускаем

    if fname.endswith(".xls"):
        await message.answer(
            "Файл в старом формате .xls — сохрани как .xlsx (Файл → Сохранить как → Excel 2007-365) и отправь снова."
        )
        return

    subject = (message.caption or "").strip()
    if not subject:
        run_task(message.from_user.id, _run_wb_create_excel(message, doc.file_id), message=message)
        return

    run_task(message.from_user.id, _handle_wb_excel(message, subject, doc.file_id), message=message)


@router.message(Command("wb_batch"))
async def cmd_wb_batch(message: Message):
    text = message.text or ""
    raw_lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    # Первая строка: /wb_batch <Субъект>
    first_parts = raw_lines[0].split(None, 1) if raw_lines else []
    subject = first_parts[1].strip() if len(first_parts) > 1 else ""
    product_lines_raw = raw_lines[1:]

    if not subject or not product_lines_raw:
        await message.answer(_HELP)
        return

    lines = [_split_line(r) for r in product_lines_raw]
    lines = [(art, name) for art, name in lines if name]

    if not lines:
        await message.answer(_HELP)
        return

    run_task(message.from_user.id, _run_wb_batch(message, subject, lines), message=message)


# ══════════════════════════════════════════════════════════════════════
# /wb_create — создание карточек WB С НУЛЯ через живой Content API
# (cards/upload), без Excel. Категория определяется автоматически.
# ══════════════════════════════════════════════════════════════════════

_CREATE_HELP = (
    "Создание карточек WB с нуля (живой API):\n"
    "/wb_create АРТИКУЛ Название товара\n\n"
    "Можно несколько строк — по товару на строку:\n"
    "/wb_create ABC-123 Ноутбук Lenovo IdeaPad Slim 3 15.6\n"
    "XYZ-9 Мышь Logitech M185 беспроводная\n\n"
    "Артикул — первое «слово» строки (без пробелов внутри), дальше название.\n"
    "Категорию WB бот определит сам. Товары обрабатываются строго по одному.\n"
    "/cancel — остановить"
)


def _split_create_line(raw: str) -> tuple[str, str]:
    """Артикул = первый токен, остальное — название.
    Режем по ПЕРВОМУ пробелу любой длины (не ждём «2+ подряд» — в реальных
    названиях с техническими характеристиками, склеенными из колонок Excel,
    двойной пробел может случайно оказаться в середине названия, а не на
    границе артикул/название, см. батч 14.07.2026: артикул захватывал
    полстроки характеристик). Артикулы WB — компактные alnum-коды без
    пробелов внутри, поэтому первый пробел — всегда верная граница."""
    if "\t" in raw:
        art, _, name = raw.partition("\t")
        return art.strip(), name.strip()
    parts = raw.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", raw.strip()


_HEALTH_CHECK_EVERY = 7


async def _health_snapshot() -> str:
    """RAM/GPU снимок для периодических чек-инов в долгих батчах."""
    import psutil
    vm = psutil.virtual_memory()
    line = f"RAM свободно {vm.available / 1024**3:.1f}/{vm.total / 1024**3:.1f}ГБ"
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        used, total, util = (x.strip() for x in out.decode().strip().split(","))
        line += f", GPU {used}/{total}МБ ({util}%)"
    except Exception:
        pass
    return line


async def _run_wb_create(message: Message, items: list[tuple[str, str]]):
    from services.wb_create import create_one, explain_error

    user_id = message.from_user.id
    llm = await get_llm(user_id)
    total = len(items)
    ok, failed = 0, 0
    # 20.07.2026: кэш на время батча — цветовые варианты одной модели
    # переиспользуют веб-поиск/категорию базовой позиции (см. wb_create.py).
    group_cache: dict = {}

    await message.answer(f"WB Create: {total} товаров, живой API. Строго по одному, без параллельных браузеров.")
    created: list[dict] = []
    queued = 0

    for idx, (article, name) in enumerate(items, 1):
        from services.llm.gemini_provider import quota_exhausted_all
        if quota_exhausted_all():
            rest = items[idx - 1:]
            queue_save(message, rest)
            queued = len(rest)
            await message.answer(f"⏸ Лимит ИИ на сегодня исчерпан — {queued} товаров в очереди, продолжу сам. /wb_queue")
            break
        # 17.07.2026: имя/артикул — сырой текст из Excel/пользователя, а у бота
        # глобально parse_mode=HTML. Без экранирования "<" в названии (например
        # "адаптер <2м") ломает разбор entity, message.answer падает
        # необработанным исключением, и весь fire-and-forget цикл молча умирает
        # на первом же товаре — выглядит как зависание батча.
        safe_name = html.escape(name)
        safe_article = html.escape(article)
        try:
            await message.answer(f"[{idx}/{total}] {safe_name} (артикул {safe_article})")
        except Exception as e:
            # 28.07.2026: этот send раньше был вне try/except — транзиентный
            # сбой Telegram здесь молча убивал весь fire-and-forget батч
            # (см. incident 16.07, тот же класс бага, другой триггер).
            log.warning(f"wb_create [{article}] announce send failed: {e}")
        try:
            res = await create_one(article, name, llm, user_id, message.answer,
                                   group_cache=group_cache)
            ok += 1
            created.append({"article": article, "nm_id": res.get("nm_id"), "subject": res.get("subject")})
            await message.answer(
                f"✅ [{idx}/{total}] Создано: {html.escape(res['summary'])}"
            )
        except Exception as e:
            from services.llm.gemini_provider import QUOTA_MARKER
            if QUOTA_MARKER in str(e):
                # 01.10.2026: дневной лимит всех моделей Gemini — остаток списка в очередь на завтра,
                # бот сам продолжит после сброса лимита (main.py: _wb_queue_loop)
                rest = items[idx - 1:]
                queue_save(message, rest)
                queued = len(rest)
                await message.answer(
                    f"⏸ [{idx}/{total}] Кончился дневной лимит ИИ (все модели Gemini). "
                    f"Оставшиеся {queued} товаров поставлены в очередь — продолжу сам после сброса лимита "
                    f"(~14:00 по Бишкеку). /wb_queue — посмотреть очередь."
                )
                break
            failed += 1
            log.error(f"wb_create [{article}] {name}: {e}", exc_info=True)
            explanation = await explain_error(article, e)
            await message.answer(
                f"🔴 [{idx}/{total}] {safe_name}: {html.escape(explanation)}"
            )
            from utils.billing import save_wb_create_failure
            from utils import hw_stats
            await save_wb_create_failure(user_id, article, name, str(e), hw=await hw_stats.snapshot())

        if idx % _HEALTH_CHECK_EVERY == 0 and idx < total:
            try:
                health = await _health_snapshot()
                await message.answer(
                    f"Чек на {idx}/{total}: ✅ {ok} 🔴 {failed}. {health}."
                )
            except Exception as e:
                log.warning(f"wb_create health check-in [{idx}/{total}] failed: {e}")

    await message.answer(
        f"WB Create готов: ✅ {ok} / 🔴 {failed} из {total}." + (f" ⏸ в очереди: {queued}." if queued else "")
    )

    # 01.10.2026: доп. ступени — документ соответствия по бренду+предмету и бесплатная инфографика.
    # Каждая необязательна: не вышло — карточка остаётся как создана.
    if created:
        from services.wb_autoextras import post_create

        async def _send_photo(path, caption):
            from aiogram.types import FSInputFile
            await message.answer_photo(FSInputFile(path), caption=caption)

        try:
            await post_create(created, message.answer, _send_photo)
        except Exception as e:
            log.error(f"post_create: {e}", exc_info=True)
            await message.answer(f"Доп. шаги (документ/инфографика) прерваны: {html.escape(str(e)[:200])}")


# ── очередь /wb_create на завтра (01.10.2026) ──────────────────────────
QUEUE_FILE = Path(__file__).resolve().parent.parent / "data" / "wb_create_queue.json"


def queue_save(message, items):
    import json as _json
    old = []
    if QUEUE_FILE.exists():
        try:
            old = _json.loads(QUEUE_FILE.read_text(encoding="utf-8")).get("items", [])
        except Exception:
            old = []
    data = {"chat_id": message.chat.id, "user_id": message.from_user.id,
            "items": [list(x) for x in items] + [x for x in old if x not in [list(i) for i in items]]}
    QUEUE_FILE.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def queue_load():
    import json as _json
    if not QUEUE_FILE.exists():
        return None
    try:
        return _json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


class ChatProxy:
    """Мини-«сообщение» для фонового продолжения очереди: answer() шлёт в нужный чат."""
    def __init__(self, bot, chat_id, user_id):
        from types import SimpleNamespace
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)

    async def answer(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, **kw)

    async def answer_photo(self, photo, **kw):
        return await self.bot.send_photo(self.chat.id, photo, **kw)


_queue_running = False


async def queue_resume(bot) -> bool:
    """Запустить очередь, если она есть. True — запущена."""
    global _queue_running
    q = queue_load()
    if not q or not q.get("items") or _queue_running:
        return False
    _queue_running = True
    try:
        QUEUE_FILE.unlink(missing_ok=True)
        proxy = ChatProxy(bot, q["chat_id"], q["user_id"])
        await proxy.answer(f"▶ Продолжаю очередь WB Create: {len(q['items'])} товаров.")
        await _run_wb_create(proxy, [tuple(x) for x in q["items"]])
    finally:
        _queue_running = False
    return True


@router.message(Command("wb_queue"))
async def cmd_wb_queue(message: Message):
    arg = (message.text or "").split(None, 1)[1].strip().lower() if len((message.text or "").split(None, 1)) > 1 else ""
    q = queue_load()
    if arg == "clear":
        QUEUE_FILE.unlink(missing_ok=True)
        await message.answer("Очередь WB Create очищена.")
        return
    if arg == "run":
        if not q:
            await message.answer("Очередь пуста.")
            return
        asyncio.create_task(queue_resume(message.bot))
        await message.answer("Запускаю очередь сейчас (если лимит ИИ ещё не сброшен — товары вернутся в очередь).")
        return
    if not q or not q.get("items"):
        await message.answer("Очередь WB Create пуста.")
        return
    preview = "\n".join(f"• {html.escape(a)} {html.escape(n[:60])}" for a, n in q["items"][:15])
    await message.answer(f"В очереди {len(q['items'])} товаров (продолжу сам после сброса лимита ИИ):\n{preview}"
                         f"\n\n/wb_queue run — запустить сейчас, /wb_queue clear — очистить.")


@router.message(Command("wb_create"))
async def cmd_wb_create(message: Message):
    text = message.text or ""
    payload = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    if not payload:
        await message.answer(_CREATE_HELP)
        return

    items = []
    for raw in payload.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        art, name = _split_create_line(raw)
        if art and name:
            items.append((art, name))

    if not items:
        await message.answer(_CREATE_HELP)
        return

    run_task(message.from_user.id, _run_wb_create(message, items), message=message)


@router.message(Command("wb_today"))
async def cmd_wb_today(message: Message):
    """Сколько карточек WB создано за сегодняшний рабочий день (09:30-18:30 Алматы)."""
    from utils.billing import count_wb_cards_today
    count = await count_wb_cards_today()
    await message.answer(
        f"📦 Создано за сегодня (09:30–18:30): <b>{count}</b> карточек WB."
    )
