import logging
from database import CostsRepository, WbStatsRepository, db_connect
from services.llm.base import LLMResponse

log = logging.getLogger(__name__)


async def save_cost(
    user_id: int,
    operation: str,
    response: LLMResponse | None = None,
    exa_requests: int = 0,
    gemini_images: int = 0,
):
    """Записывает стоимость операции в БД.

    Обёрнуто в try/except: сбой записи цены (например, БД временно занята)
    не должен обрывать остаток пайплайна создания карточки — теряем только
    строчку статистики, не саму карточку."""
    try:
        async with db_connect() as db:
            await CostsRepository(db).add(
                user_id,
                operation,
                provider=response.provider if response else "",
                input_tokens=response.input_tokens if response else 0,
                output_tokens=response.output_tokens if response else 0,
                usd=response.usd if response else 0.0,
                exa_requests=exa_requests,
                gemini_images=gemini_images,
            )
    except Exception as e:
        log.warning(f"save_cost не записался ({operation}): {e}")


async def save_wb_card_created(
    user_id: int,
    article: str,
    nm_id: int,
    title: str,
    subject: str,
    photos_count: int = 0,
    elapsed_sec: float | None = None,
    hw: dict | None = None,
):
    """Записывает успешно созданную карточку WB в БД — для подсчёта по рабочим
    дням (/wb_today) и для дневного xlsx-отчёта (utils/daily_xlsx_report.py),
    который сводит elapsed_sec/hw по категориям для поиска мест, где стоит
    оптимизировать код (см. utils/hw_stats.py). Как save_cost: сбой записи
    не должен ронять создание карточки, теряем только строчку статистики."""
    try:
        async with db_connect() as db:
            await WbStatsRepository(db).add_created(
                user_id, article, nm_id, title, subject, photos_count, elapsed_sec, hw)
    except Exception as e:
        log.warning(f"save_wb_card_created не записался (nm_id={nm_id}): {e}")


async def save_wb_create_failure(user_id: int, article: str, name: str, error: str,
                                  subject: str = "", hw: dict | None = None):
    """Записывает позицию /wb_create, упавшую с исключением (в т.ч. «карточка
    не появилась на WB» — часто ложный провал: карточка реально создалась, но
    WB проиндексировал её позже 135с опроса, см. wb_create.py). Вечерний отчёт
    (main.py _build_wb_daily_report) перепроверяет эти записи живьём на WB —
    найденные считаются успехом, не найденные идут в список на доработку."""
    try:
        async with db_connect() as db:
            await WbStatsRepository(db).add_failure(user_id, article, name, error, subject, hw)
    except Exception as e:
        log.warning(f"save_wb_create_failure не записался (article={article}): {e}")


async def save_wb_tnved_miss(user_id: int, article: str, subject_id: int,
                              subject_name: str, product_name: str) -> None:
    """Записывает случай, когда для карточки не нашёлся код ТН ВЭД ни в
    официальном справочнике WB, ни в своей библиотеке (wb_tnved_codes) —
    карточка ушла БЕЗ ТН ВЭД, пользователь предупреждён в чат сразу, но
    04.08.2026 по просьбе пользователя это дополнительно копится сюда для
    сводки в вечернем отчёте (см. _build_wb_daily_report, main.py) —
    единичное сообщение в чате легко пропустить среди батча из 20+ карточек."""
    try:
        async with db_connect() as db:
            await WbStatsRepository(db).add_tnved_miss(
                user_id, article, subject_id, subject_name, product_name)
    except Exception as e:
        log.warning(f"save_wb_tnved_miss не записался (article={article}): {e}")


async def get_wb_tnved_misses_today() -> list[tuple[str, int, str, str]]:
    """(article, subject_id, subject_name, product_name) для карточек,
    ушедших сегодня без ТН ВЭД в рабочем окне 09:30-18:30 Алматы — см.
    save_wb_tnved_miss."""
    async with db_connect() as db:
        rows = await WbStatsRepository(db).tnved_misses_in_work_window()
    return [(r["article"], r["subject_id"], r["subject_name"], r["product_name"]) for r in rows]


async def get_wb_create_failures_today() -> list[tuple[str, str, str]]:
    """(article, name, error) для позиций /wb_create, упавших сегодня в
    рабочем окне 09:30-18:30 Алматы — см. save_wb_create_failure."""
    async with db_connect() as db:
        rows = await WbStatsRepository(db).failures_in_work_window()
    return [(r["article"], r["name"], r["error"]) for r in rows]


async def get_wb_cards_today() -> list[tuple[str, int, str]]:
    """Те же карточки, что считает count_wb_cards_today() (то же рабочее
    окно 09:30-18:30 Алматы), но целиком — (article, nm_id, title) —
    для вечерней сверки с живым WB (см. verify_articles_live в
    services/wb_content.py: бот иногда сдаётся ждать появления карточки
    раньше, чем WB её реально проиндексирует, и репортит ложный провал —
    23.07.2026, батч акустики 2E)."""
    async with db_connect() as db:
        rows = await WbStatsRepository(db).created_in_work_window()
    return [(r["article"], r["nm_id"], r["title"]) for r in rows]


async def get_wb_cards_today_full() -> list[dict]:
    """Как get_wb_cards_today(), но со всеми колонками — для дневного
    xlsx-отчёта (лист «Созданные» + лист «Нагрузка на ПК»,
    utils/daily_xlsx_report.py)."""
    async with db_connect() as db:
        return await WbStatsRepository(db).created_in_work_window()


async def get_wb_create_failures_today_full() -> list[dict]:
    """Как get_wb_create_failures_today(), но со всеми колонками — для
    дневного xlsx-отчёта (лист «Отклонённые», utils/daily_xlsx_report.py)."""
    async with db_connect() as db:
        return await WbStatsRepository(db).failures_in_work_window()


async def count_wb_cards_today() -> int:
    """Считает карточки WB, созданные сегодня в рабочем окне 09:30-18:30
    по Алматы (UTC+5, без перехода на летнее время — сдвиг фиксированный).
    "Сегодня" — тоже по алматинскому календарному дню, а не по UTC."""
    async with db_connect() as db:
        return await WbStatsRepository(db).count_created_in_work_window()


async def get_wb_created_today() -> list[dict]:
    """Полный список карточек WB, успешно созданных сегодня с 09:30 по
    Алматы (без верхней границы — используется вечерним отчётом в 17:50,
    а не только для готового рабочего окна, см. count_wb_cards_today)."""
    async with db_connect() as db:
        return await WbStatsRepository(db).created_since_work_start()


def format_cost(response: LLMResponse, exa: int = 0, gemini: int = 0) -> str:
    """Форматирует строку стоимости для отправки пользователю."""
    parts = [f"{response.provider}: ${response.usd:.4f} ({response.input_tokens}+{response.output_tokens} тк)"]
    if exa:
        parts.append(f"Exa: {exa} запросов")
    if gemini:
        parts.append(f"Gemini: {gemini} изображений")
    return " | ".join(parts)
