import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from config import settings

from .migrations import LATEST_VERSION, migrate

log = logging.getLogger(__name__)


@asynccontextmanager
async def db_connect(path: str | None = None):
    """Соединение с БД с настройками под конкурентный доступ.

    Без busy_timeout параллельные save_cost() при батче из 15-20+ карточек
    периодически ловили "database is locked" и молча теряли запись траты
    (обнаружено 15.07: 30 из 209 вызовов DeepSeek за день не попали в costs).
    busy_timeout заставляет писателя ждать освобождения блокировки вместо
    немедленной ошибки; WAL позволяет читателям работать, пока идёт запись.
    """
    path = path or settings.db_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA busy_timeout = 10000")
        await db.execute("PRAGMA journal_mode = WAL")
        yield db


async def init_db(path: str | None = None) -> None:
    """Приводит схему базы к последней версии (см. database/migrations.py)."""
    async with db_connect(path) as db:
        applied = await migrate(db)
    if applied:
        log.info("Схема БД обновлена до версии %s (применены: %s)", LATEST_VERSION, applied)
