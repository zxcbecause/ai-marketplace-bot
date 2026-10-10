"""Версионированные миграции схемы SQLite.

Текущая версия схемы хранится в `PRAGMA user_version`. При старте бот применяет
по порядку все миграции с номером больше текущего; каждая выполняется в своей
транзакции вместе с повышением версии, поэтому прерванная миграция не оставляет
базу в полусостоянии.

Как добавить изменение схемы: допишите в MIGRATIONS новый элемент со следующим
номером. Уже выпущенные миграции не редактируются.
"""
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    # для базовой миграции: ALTER ... ADD COLUMN на старых базах, где колонка уже есть
    tolerate_duplicate_columns: bool = False


_BASELINE = (
    """CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        is_admin    INTEGER DEFAULT 0,
        llm_model   TEXT    DEFAULT 'deepseek',
        created_at  TEXT    DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS card_cache (
        user_id     INTEGER PRIMARY KEY,
        product     TEXT,
        brand       TEXT,
        model       TEXT,
        color       TEXT,
        color_en    TEXT,
        context     TEXT,
        category    TEXT,
        last_action TEXT    DEFAULT 'card',
        updated_at  TEXT    DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS last_result (
        user_id     INTEGER PRIMARY KEY,
        action      TEXT,
        text        TEXT,
        updated_at  TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS costs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        operation       TEXT,
        provider        TEXT,
        input_tokens    INTEGER DEFAULT 0,
        output_tokens   INTEGER DEFAULT 0,
        usd             REAL    DEFAULT 0.0,
        exa_requests    INTEGER DEFAULT 0,
        gemini_images   INTEGER DEFAULT 0,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS batch_items (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER NOT NULL,
        message_id          INTEGER,
        richcontent_msg_id  INTEGER,
        product             TEXT,
        brand               TEXT,
        model               TEXT,
        color               TEXT,
        color_en            TEXT,
        category            TEXT,
        features            TEXT,
        slogan              TEXT,
        tips                TEXT,
        rich_slogan         TEXT,
        gaming_accent       TEXT,
        user_style          TEXT,
        used_photo_urls     TEXT,
        created_at          TEXT    DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_batch_items_msg ON batch_items(user_id, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_items_rc_msg ON batch_items(user_id, richcontent_msg_id)",
    """CREATE TABLE IF NOT EXISTS tnved_codes (
        category_key    TEXT PRIMARY KEY,
        tnved           TEXT NOT NULL,
        note            TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS wb_tnved_codes (
        subject_id      INTEGER PRIMARY KEY,
        subject_name    TEXT,
        tnved           TEXT NOT NULL,
        note            TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS wb_dims_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id      INTEGER NOT NULL,
        length          REAL NOT NULL,
        width           REAL NOT NULL,
        height          REAL NOT NULL,
        weight          REAL NOT NULL,
        source          TEXT NOT NULL DEFAULT '',
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wb_dims_history_subject ON wb_dims_history(subject_id)",
    """CREATE TABLE IF NOT EXISTS wb_tnved_candidates (
        subject_id      INTEGER NOT NULL,
        tnved           TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        subject_name    TEXT NOT NULL DEFAULT '',
        source          TEXT NOT NULL DEFAULT '',
        updated_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (subject_id, tnved)
    )""",
    """CREATE TABLE IF NOT EXISTS wb_tnved_misses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        article         TEXT,
        subject_id      INTEGER,
        subject_name    TEXT,
        product_name    TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wb_tnved_misses_created_at ON wb_tnved_misses(created_at)",
    """CREATE TABLE IF NOT EXISTS search_cache (
        cache_key       TEXT PRIMARY KEY,
        payload         TEXT NOT NULL,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS wb_created_cards (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        article         TEXT,
        nm_id           INTEGER,
        title           TEXT,
        subject         TEXT,
        photos_count    INTEGER DEFAULT 0,
        elapsed_sec     REAL,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wb_created_cards_created_at ON wb_created_cards(created_at)",
    """CREATE TABLE IF NOT EXISTS wb_create_failures (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        article         TEXT,
        name            TEXT,
        error           TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wb_create_failures_created_at ON wb_create_failures(created_at)",
    # колонки, которые раньше добавлялись ALTER'ами при каждом старте
    "ALTER TABLE card_cache ADD COLUMN last_action TEXT DEFAULT 'card'",
    "ALTER TABLE card_cache ADD COLUMN updated_at TEXT",
    "ALTER TABLE card_cache ADD COLUMN brand TEXT",
    "ALTER TABLE card_cache ADD COLUMN model TEXT",
    "ALTER TABLE card_cache ADD COLUMN color TEXT",
    "ALTER TABLE card_cache ADD COLUMN color_en TEXT",
    "ALTER TABLE users ADD COLUMN infographic_style TEXT DEFAULT 'default'",
    "ALTER TABLE wb_created_cards ADD COLUMN ram_free_gb REAL",
    "ALTER TABLE wb_created_cards ADD COLUMN gpu_used_mb REAL",
    "ALTER TABLE wb_created_cards ADD COLUMN gpu_total_mb REAL",
    "ALTER TABLE wb_created_cards ADD COLUMN gpu_util_pct REAL",
    "ALTER TABLE wb_create_failures ADD COLUMN subject TEXT",
    "ALTER TABLE wb_create_failures ADD COLUMN ram_free_gb REAL",
    "ALTER TABLE wb_create_failures ADD COLUMN gpu_used_mb REAL",
    "ALTER TABLE wb_create_failures ADD COLUMN gpu_total_mb REAL",
    "ALTER TABLE wb_create_failures ADD COLUMN gpu_util_pct REAL",
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline schema", _BASELINE, tolerate_duplicate_columns=True),
    Migration(2, "indexes for /costs reports", (
        "CREATE INDEX IF NOT EXISTS idx_costs_created_at ON costs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_costs_user_id ON costs(user_id)",
    )),
)

LATEST_VERSION = MIGRATIONS[-1].version


async def get_version(db) -> int:
    cur = await db.execute("PRAGMA user_version")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def migrate(db, migrations: tuple[Migration, ...] = MIGRATIONS) -> list[int]:
    """Применяет недостающие миграции. Возвращает номера применённых."""
    versions = [m.version for m in migrations]
    if versions != sorted(set(versions)):
        raise ValueError("номера миграций должны строго возрастать")

    current = await get_version(db)
    applied = []
    # Транзакции открываем сами явным BEGIN: тогда и DDL (CREATE/ALTER) откатывается
    # вместе с остальной миграцией. Незакрытую транзакцию вызывающего фиксируем заранее.
    await db.commit()
    for m in migrations:
        if m.version <= current:
            continue
        await db.execute("BEGIN")
        try:
            for sql in m.statements:
                try:
                    await db.execute(sql)
                except Exception as e:
                    if m.tolerate_duplicate_columns and "duplicate column" in str(e).lower():
                        continue
                    raise
            await db.execute(f"PRAGMA user_version = {int(m.version)}")
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            log.error("Миграция %s (%s) не применилась, база осталась на версии %s",
                      m.version, m.name, current)
            raise
        current = m.version
        applied.append(m.version)
        log.info("Миграция схемы %s применена: %s", m.version, m.name)
    return applied
