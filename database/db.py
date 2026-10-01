import logging
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from config import settings

log = logging.getLogger(__name__)

DB_PATH = settings.db_path


@asynccontextmanager
async def db_connect():
    """Соединение с БД с настройками под конкурентный доступ.

    Без busy_timeout параллельные save_cost() при батче из 15-20+ карточек
    периодически ловили "database is locked" и молча теряли запись траты
    (обнаружено 15.07: 30 из 209 вызовов DeepSeek за день не попали в costs).
    busy_timeout заставляет писателя ждать освобождения блокировки вместо
    немедленной ошибки; WAL позволяет читателям работать, пока идёт запись.
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 10000")
        await db.execute("PRAGMA journal_mode = WAL")
        yield db


async def init_db():
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row

        # Создаём таблицы
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                is_admin    INTEGER DEFAULT 0,
                llm_model   TEXT    DEFAULT 'deepseek',
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS card_cache (
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
            );

            CREATE TABLE IF NOT EXISTS last_result (
                user_id     INTEGER PRIMARY KEY,
                action      TEXT,
                text        TEXT,
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS costs (
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
            );

            CREATE TABLE IF NOT EXISTS batch_items (
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
            );
            CREATE INDEX IF NOT EXISTS idx_batch_items_msg
                ON batch_items(user_id, message_id);
            CREATE INDEX IF NOT EXISTS idx_batch_items_rc_msg
                ON batch_items(user_id, richcontent_msg_id);

            CREATE TABLE IF NOT EXISTS tnved_codes (
                category_key    TEXT PRIMARY KEY,
                tnved           TEXT NOT NULL,
                note            TEXT,
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS wb_tnved_codes (
                subject_id      INTEGER PRIMARY KEY,
                subject_name    TEXT,
                tnved           TEXT NOT NULL,
                note            TEXT,
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS wb_dims_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id      INTEGER NOT NULL,
                length          REAL NOT NULL,
                width           REAL NOT NULL,
                height          REAL NOT NULL,
                weight          REAL NOT NULL,
                source          TEXT NOT NULL DEFAULT '',
                created_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_wb_dims_history_subject
                ON wb_dims_history(subject_id);

            CREATE TABLE IF NOT EXISTS wb_tnved_candidates (
                subject_id      INTEGER NOT NULL,
                tnved           TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                subject_name    TEXT NOT NULL DEFAULT '',
                source          TEXT NOT NULL DEFAULT '',
                updated_at      TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (subject_id, tnved)
            );

            CREATE TABLE IF NOT EXISTS wb_tnved_misses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                article         TEXT,
                subject_id      INTEGER,
                subject_name    TEXT,
                product_name    TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_wb_tnved_misses_created_at
                ON wb_tnved_misses(created_at);

            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key       TEXT PRIMARY KEY,
                payload         TEXT NOT NULL,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS wb_created_cards (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                article         TEXT,
                nm_id           INTEGER,
                title           TEXT,
                subject         TEXT,
                photos_count    INTEGER DEFAULT 0,
                elapsed_sec     REAL,
                created_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_wb_created_cards_created_at
                ON wb_created_cards(created_at);

            CREATE TABLE IF NOT EXISTS wb_create_failures (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                article         TEXT,
                name            TEXT,
                error           TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_wb_create_failures_created_at
                ON wb_create_failures(created_at);
        """)

        # Миграции — добавляем колонки если их нет (для существующих БД)
        migrations = [
            "ALTER TABLE card_cache ADD COLUMN last_action TEXT DEFAULT 'card'",
            "ALTER TABLE card_cache ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
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
        ]
        for sql in migrations:
            try:
                await db.execute(sql)
            except Exception as e:
                # Раньше глушили ЛЮБУЮ ошибку без лога — неотличимо от
                # штатного "колонка уже есть" была бы опечатка в SQL,
                # отсутствие таблицы или повреждение файла БД. Игнорируем
                # молча только реальный "duplicate column", остальное — в лог.
                if "duplicate column" not in str(e).lower():
                    log.error(f"Миграция не применилась: {sql!r}: {e}")

        await db.commit()
