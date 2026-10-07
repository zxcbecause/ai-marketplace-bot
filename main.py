import asyncio
import logging
import ssl
import os
import sys
import datetime

# SSL-патч нужен только на рабочем Windows-ПК, где антивирус (Avast) перехватывает HTTPS.
# В Docker и на серверах проверка сертификатов остаётся включённой.
if os.name == "nt" or os.environ.get("DISABLE_SSL_VERIFY") == "1":
    ssl._create_default_https_context = ssl._create_unverified_context
    os.environ["PYTHONHTTPSVERIFY"] = "0"

    import urllib3
    urllib3.disable_warnings()

import pytz
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from config import settings
from database import init_db, db_connect
from handlers import common, admin, card, photo, fix, wb, video, self_destruct
from middleware.access import AccessMiddleware
from utils.commands import set_commands
from utils.runtime_env import IS_WINDOWS, acquire_singleton_lock

# ── Hello Goodbye (Beatles) — гифка каждый день в 18:00, удаляется через 5 мин ──
_HELLO_GIF_ID  = "CgACAgQAAxkBAAOvaf-vM-NbOOiHMqPc8l-yTI-o7kAAAi4IAAIDPHRTgi9DppbclpE7BA"
_HELLO_TEXT    = (
    'You say, "Goodbye", and I say, "Hello\n'
    'Hello, hello"\n'
    'I don\'t know why you say, "Goodbye", I say, "Hello\n'
    'Hello, hello"\n'
    'I don\'t know why you say, "Goodbye", I say "Hello"'
)


# ── Защита от двойного запуска (03.08.2026) ────────────────────────────
# Живой инцидент: два инстанса main.py одновременно боролись за getUpdates —
# 104 подряд TelegramConflictError, ~2 минуты бот не принимал сообщения,
# LLM-вызовы шли параллельно с обоих (риск задвоенной оплаты). Эксклюзивный лок
# привязан к файловому хендлу процесса (msvcrt на Windows, flock на Linux) — ОС
# освобождает его сама даже при форс-килле, поэтому протухший лок от
# упавшего процесса не блокирует следующий legit-запуск.
_lock_file_handle = None


def _acquire_singleton_lock() -> None:
    global _lock_file_handle
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot.lock")
    _lock_file_handle = acquire_singleton_lock(lock_path)
    if _lock_file_handle is None:
        print(
            "Другой экземпляр бота уже запущен (data/bot.lock занят другим процессом). "
            "Завершаюсь, чтобы не конфликтовать за getUpdates.",
            file=sys.stderr,
        )
        sys.exit(1)


async def _delete_after(bot, chat_id: int, message_id: int, delay: int = 300):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _split_real_failures(failures: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Отсеивает из списка сегодняшних провалов /wb_create те, что на самом
    деле НЕ провал — карточка «уже существует на WB» (см. wb_create.py) —
    это штатный пропуск дубля, а не ошибка создания. Перепроверка живьём
    (verify_articles_live) для такого артикула ВСЕГДА находит карточку
    (она может быть создана много дней назад) и ошибочно засчитывает её как
    «сегодняшний ложный провал» — раздувает дневной отчёт и дублирует часть
    артикулов (несколько попыток пересоздать один и тот же существующий
    товар за день = несколько строк-провалов). Заодно дедуп по артикулу —
    та же повторная попытка не должна давать 2 строки."""
    seen: set[str] = set()
    result = []
    for article, name, error in failures:
        if "уже существует на WB" in error or "уже создан на WB" in error:
            continue
        if article in seen:
            continue
        seen.add(article)
        result.append((article, name, error))
    return result


async def _build_wb_daily_report() -> str:
    """Собирает текст вечернего отчёта по /wb_create за сегодня: список точно
    подтверждённых карточек + отдельно список тех, что упали или не
    дождались подтверждения — эти дополнительно перепроверяются живьём на
    WB (readonly-ключ), т.к. «не появилась за 135с» часто значит просто
    задержку индексации, а не реальный провал (см. save_wb_create_failure,
    23.07.2026 — батч акустики 2E, 3 подряд ложных провала). «Уже существует
    на WB» отсеивается ДО перепроверки (см. _split_real_failures) — это не
    провал сегодняшнего создания, а старая карточка."""
    import html as _html
    from utils.billing import get_wb_cards_today, get_wb_create_failures_today, get_wb_tnved_misses_today
    from services.wb_content import verify_articles_live

    confirmed = await get_wb_cards_today()  # (article, nm_id, title) — уже точно на WB
    failures = _split_real_failures(await get_wb_create_failures_today())  # (article, name, error)
    tnved_misses = await get_wb_tnved_misses_today()  # (article, subject_id, subject_name, product_name)

    lines = [f"📦 За сегодня подтверждено на WB: <b>{len(confirmed)}</b> карточек."]

    if confirmed:
        lines.append("\n<b>Точно созданы:</b>")
        for article, nm_id, title in confirmed:
            lines.append(f"✅ {nm_id} — {_html.escape(title or article)} (арт. {_html.escape(article)})")

    if failures:
        recheck = await verify_articles_live([a for a, _, _ in failures])
        still_bad = [(a, n, e) for a, n, e in failures if not recheck.get(a)]
        recovered = [(a, n, recheck[a]) for a, n, e in failures if recheck.get(a)]

        if recovered:
            lines.append(f"\n<b>Нашлись при перепроверке (ложный провал, {len(recovered)} шт.):</b>")
            for article, name, nm_id in recovered:
                lines.append(f"✅ {nm_id} — {_html.escape(name)} (арт. {_html.escape(article)})")

        if still_bad:
            lines.append(f"\n<b>⚠️ На доработку/проверку ({len(still_bad)} шт.):</b>")
            for article, name, error in still_bad:
                lines.append(f"🔴 {_html.escape(article)} — {_html.escape(name)}: {_html.escape(error[:150])}")

    if tnved_misses:
        # Группируем по категории — один и тот же subject у пачки цветовых
        # вариантов не должен повторяться строка в строку (см.
        # save_wb_tnved_miss, 04.08.2026).
        by_subject: dict[tuple[int, str], list[str]] = {}
        for article, subject_id, subject_name, _product_name in tnved_misses:
            by_subject.setdefault((subject_id, subject_name), []).append(article)
        lines.append(f"\n<b>⚠️ Без кода ТН ВЭД ({len(tnved_misses)} шт., {len(by_subject)} категорий):</b>")
        for (subject_id, subject_name), articles in by_subject.items():
            arts = ", ".join(_html.escape(a) for a in articles[:5])
            more = f" и ещё {len(articles) - 5}" if len(articles) > 5 else ""
            lines.append(f"🟡 {_html.escape(subject_name)} (id {subject_id}) — {arts}{more}")

    return "\n".join(lines)


def _chunk_text(text: str, limit: int = 3500) -> list[str]:
    """Telegram режет сообщения на 4096 символов — бьём по строкам, не
    по границе тегов, чтобы не порвать HTML-разметку внутри строки."""
    lines = text.split("\n")
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


async def _send_hello_goodbye(bot):
    """Отправляет гифку всем пользователям, удаляет через 5 минут."""
    log = logging.getLogger("scheduler")
    async with db_connect() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT user_id FROM users")
        users = [r["user_id"] for r in await cur.fetchall()]
    if not users:
        log.info("hello_goodbye: нет пользователей")
        return
    log.info(f"hello_goodbye: отправка {len(users)} пользователям")

    try:
        report_text = await _build_wb_daily_report()
    except Exception as e:
        log.warning(f"wb_daily_report failed: {e}")
        from utils.billing import count_wb_cards_today
        report_text = f"📦 За сегодня создано: <b>{await count_wb_cards_today()}</b> карточек WB."

    xlsx_bytes, xlsx_summary, xlsx_doc = None, None, None
    try:
        from utils.daily_xlsx_report import build_daily_xlsx_report
        xlsx_bytes, xlsx_summary = await build_daily_xlsx_report()
        if xlsx_summary["created"] or xlsx_summary["failed"] or xlsx_summary["infographic_uploaded_today"]:
            date_str = datetime.datetime.now(pytz.timezone("Asia/Almaty")).strftime("%Y-%m-%d")
            xlsx_doc = BufferedInputFile(xlsx_bytes, filename=f"wb_report_{date_str}.xlsx")
    except Exception as e:
        log.warning(f"daily_xlsx_report failed: {e}", exc_info=True)

    for user_id in users:
        # 28.07.2026: гифка — необязательное украшение, а report_text/xlsx_doc —
        # то, ради чего вечерний отчёт вообще существует. Раньше send_animation
        # стоял первым в общем try — протухший _HELLO_GIF_ID («wrong file
        # identifier») ронял исключение ДО отправки текста/xlsx, и весь отчёт
        # молча не уходил ни разу с 24.07 по 27.07 (см. bot.log, hello_goodbye
        # error). Теперь гифка — отдельный try, не блокирует отчёт.
        try:
            sent = await bot.send_animation(
                chat_id=user_id,
                animation=_HELLO_GIF_ID,
                caption=_HELLO_TEXT,
            )
            asyncio.create_task(_delete_after(bot, user_id, sent.message_id, 300))
        except Exception as e:
            log.warning(f"hello_goodbye animation failed (user {user_id}): {e}")

        try:
            for chunk in _chunk_text(report_text):
                await bot.send_message(user_id, chunk)
            if xlsx_doc is not None:
                await bot.send_document(
                    user_id, xlsx_doc,
                    caption=(f"Созданные / Отклонённые / Не успели / Нагрузка на ПК / Инфографика — "
                             f"{xlsx_summary['created']} создано, {xlsx_summary['failed']} отклонено; "
                             f"инфографика: {xlsx_summary['infographic_uploaded_today']} залито сегодня, "
                             f"{xlsx_summary['infographic_pending']} ждёт заливки."),
                )
        except Exception as e:
            log.warning(f"hello_goodbye report failed (user {user_id}): {e}")


async def _scheduler(bot):
    """Фоновый планировщик: ждёт 17:55 и запускает гифку каждый день."""
    log = logging.getLogger("scheduler")
    import pytz
    tz = pytz.timezone("Asia/Almaty")
    while True:
        now = datetime.datetime.now(tz)
        target = now.replace(hour=17, minute=55, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info(f"scheduler: следующая гифка через {int(wait//3600)}ч {int((wait%3600)//60)}мин")
        await asyncio.sleep(wait)
        await _send_hello_goodbye(bot)



async def _wb_queue_loop(bot):
    """01.10.2026: продолжение очереди /wb_create после сброса дневного лимита Gemini.
    Раз в 30 минут: если очередь есть и лимит не исчерпан — запускаем; если лимит всё ещё
    кончен, первый же товар вернёт остаток в очередь (handlers/wb.py)."""
    log = logging.getLogger("wb_queue")
    from handlers.wb import queue_load, queue_resume
    from services.llm.gemini_provider import quota_exhausted_all
    await asyncio.sleep(120)
    while True:
        try:
            if queue_load() and not quota_exhausted_all():
                log.info("wb_queue: продолжаю очередь")
                await queue_resume(bot)
        except Exception as e:
            log.error(f"wb_queue: {e}", exc_info=True)
        await asyncio.sleep(1800)


async def _balance_watch_loop(bot):
    """Каждые 6 часов проверяет баланс DeepSeek; при остатке < $1
    предупреждает админа в Telegram (защита от внезапного 402 посреди батча)."""
    log = logging.getLogger("balance")
    import httpx
    _WARN_AT = 1.0
    warned = False
    while True:
        try:
            async with httpx.AsyncClient(verify=False, timeout=15) as c:
                r = await c.get(
                    "https://api.deepseek.com/user/balance",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                )
            data = r.json()
            infos = data.get("balance_infos") or [{}]
            balance = float(infos[0].get("total_balance", 0) or 0)
            available = data.get("is_available", False)
            log.info(f"balance: DeepSeek ${balance:.2f} (available={available})")
            if balance < _WARN_AT and not warned:
                await bot.send_message(
                    settings.admin_id,
                    f"⚠️ Баланс DeepSeek: ${balance:.2f} — скоро закончится, "
                    f"пополните чтобы батчи не падали с 402.",
                )
                warned = True
            elif balance >= _WARN_AT:
                warned = False  # пополнили — сбрасываем, предупредим при новом снижении
        except Exception as e:
            log.warning(f"balance check failed: {e}")
        await asyncio.sleep(6 * 3600)


async def _warmup_models():
    """Прогревает CLIP и rembg-модели фоном сразу при старте бота.

    17.07.2026 (по опыту второго ПК, отчёт report_20260716.md): без
    прогрева модели грузятся лениво прямо во время ПЕРВОГО реального
    запроса пользователя — скачивание/инициализация чекпоинта занимает
    10-16с по сети, что съедает почти весь бюджет таймаута GPU-вызова
    (run_gpu, по умолчанию 60с, но многие вызовы используют 30-45с).
    Первые же вызовы скоринга фото/вырезания фона гарантированно
    таймаутят, а зависший (не убитый) поток продолжает жить в фоне —
    несколько таких таймаутов подряд на слабой машине обвалили RAM с
    3.1ГБ до 18МБ за минуту. Прогрев до первого сообщения устраняет
    сам триггер."""
    log = logging.getLogger("warmup")
    try:
        from services.image.clip_scorer import _load as _load_clip
        from services.image.background import (
            _get_session, _get_session_u2net, _get_session_isnet,
        )
        await asyncio.to_thread(_load_clip)
        await asyncio.to_thread(_get_session)
        await asyncio.to_thread(_get_session_u2net)
        await asyncio.to_thread(_get_session_isnet)
        log.info("warmup: CLIP + rembg (birefnet/u2net/isnet) готовы")
    except Exception as e:
        log.warning(f"warmup failed: {e}")


async def _db_backup_loop():
    """Раз в сутки копирует БД в data/backups/, хранит последние 7 копий.
    Через sqlite backup API — безопасно даже при активных записях."""
    import sqlite3
    from pathlib import Path
    log = logging.getLogger("backup")

    def _do_backup():
        db_path = Path(settings.db_path)
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        dest = backup_dir / f"bot-{datetime.date.today().isoformat()}.db"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        # Храним только 7 последних
        old = sorted(backup_dir.glob("bot-*.db"))[:-7]
        for f in old:
            f.unlink(missing_ok=True)
        return dest.name

    while True:
        try:
            name = await asyncio.to_thread(_do_backup)
            log.info(f"backup: БД сохранена в data/backups/{name}")
        except Exception as e:
            log.warning(f"backup failed: {e}")
        await asyncio.sleep(24 * 3600)


def setup_logging():
    import sys
    from pathlib import Path
    Path(settings.log_path).parent.mkdir(parents=True, exist_ok=True)

    # Файловый лог всегда в utf-8, с ротацией: 5 МБ × 3 архива.
    from logging.handlers import RotatingFileHandler
    handlers = [RotatingFileHandler(
        settings.log_path, encoding="utf-8",
        maxBytes=5 * 1024 * 1024, backupCount=3,
    )]

    # Консольный лог — только если есть рабочий stderr (под pythonw его нет)
    # и принудительно в utf-8. Иначе юникод (—, →, кириллица) роняет логгер
    # на cp1251-консоли Windows и обрывает обработку карточки.
    if sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
            handlers.append(logging.StreamHandler(sys.stderr))  # только если utf-8 удалось
        except Exception:
            pass  # нет нормального stderr (pythonw) — консольный лог пропускаем

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


async def main():
    _acquire_singleton_lock()

    setup_logging()
    log = logging.getLogger(__name__)
    log.info(f"Singleton lock acquired (pid={os.getpid()})")

    # Прогрев CLIP/rembg — запускаем сразу, фоном, параллельно с остальной
    # инициализацией, чтобы модели были готовы ДО первого сообщения
    # пользователя (см. _warmup_models).
    asyncio.create_task(_warmup_models())
    log.info("Model warmup started (CLIP + rembg)")

    await init_db()
    log.info("Database initialized")

    from services.tnved import seed_from_profiles
    await seed_from_profiles()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(card.router)
    dp.include_router(photo.router)
    dp.include_router(fix.router)
    dp.include_router(wb.router)
    dp.include_router(video.router)
    if IS_WINDOWS:  # самоудаление реализовано через PowerShell — только на Windows-ПК
        dp.include_router(self_destruct.router)

    # 13.08.2026: раньше без try/except — при недоступном Telegram (ISP-блок)
    # это падало необработанным исключением и убивало весь процесс ДО
    # start_polling, хотя сам aiogram's start_polling штатно переживает
    # временную недоступность сети через свой собственный ретрай-бэкофф.
    # Watchdog просто перезапускал бота раз в 5 мин на тот же краш.
    try:
        await set_commands(bot)
        log.info("Commands registered")
    except Exception as e:
        log.warning(f"set_commands не удался (Telegram недоступен?): {e} — продолжаю без обновления меню команд")

    # Запускаем планировщик фоново
    asyncio.create_task(_scheduler(bot))
    log.info("Scheduler started (17:55 Almaty)")

    # Ежедневный бэкап БД (первый — сразу при старте)
    asyncio.create_task(_db_backup_loop())
    log.info("DB backup loop started (daily, keep 7)")

    # Очередь /wb_create на завтра (после дневного лимита ИИ)
    asyncio.create_task(_wb_queue_loop(bot))
    log.info("WB create queue loop started (30 min)")

    # Страж баланса DeepSeek (каждые 6 часов, предупреждение при < $1)
    asyncio.create_task(_balance_watch_loop(bot))
    log.info("DeepSeek balance watch started (6h, warn < $1)")

    log.info("Bot starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
