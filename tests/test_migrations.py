"""Миграции схемы на SQLite в памяти."""
import asyncio

import aiosqlite
import pytest

from database.migrations import LATEST_VERSION, MIGRATIONS, Migration, get_version, migrate


def run(coro):
    return asyncio.run(coro)


async def columns(db, table):
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in await cur.fetchall()}


async def tables(db):
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r[0] for r in await cur.fetchall()}


def test_fresh_database_reaches_latest_version():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            applied = await migrate(db)
            assert applied == [m.version for m in MIGRATIONS]
            assert await get_version(db) == LATEST_VERSION
            assert {"users", "costs", "wb_created_cards", "wb_create_failures",
                    "wb_tnved_misses", "card_cache"} <= await tables(db)
            assert "infographic_style" in await columns(db, "users")
            assert "gpu_util_pct" in await columns(db, "wb_create_failures")
    run(scenario())


def test_second_run_is_noop():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await migrate(db)
            assert await migrate(db) == []
            assert await get_version(db) == LATEST_VERSION
    run(scenario())


def test_legacy_database_without_version_is_upgraded():
    """База из старых версий бота: таблицы есть, части колонок нет, user_version = 0."""
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE users (user_id INTEGER PRIMARY KEY, is_admin INTEGER DEFAULT 0,
                                    llm_model TEXT DEFAULT 'deepseek',
                                    created_at TEXT DEFAULT (datetime('now')));
                CREATE TABLE card_cache (user_id INTEGER PRIMARY KEY, product TEXT,
                                         context TEXT, category TEXT, last_action TEXT);
                INSERT INTO users (user_id, llm_model) VALUES (42, 'gemini');
            """)
            await db.commit()
            assert await get_version(db) == 0

            await migrate(db)

            assert await get_version(db) == LATEST_VERSION
            assert {"brand", "model", "color", "color_en", "updated_at"} <= await columns(db, "card_cache")
            assert "infographic_style" in await columns(db, "users")
            cur = await db.execute("SELECT llm_model, infographic_style FROM users WHERE user_id = 42")
            assert await cur.fetchone() == ("gemini", "default")
    run(scenario())


def test_failed_migration_rolls_back_completely():
    broken = MIGRATIONS + (
        Migration(LATEST_VERSION + 1, "broken", (
            "CREATE TABLE half_done (id INTEGER)",
            "ALTER TABLE no_such_table ADD COLUMN x TEXT",
        )),
    )

    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            with pytest.raises(Exception):
                await migrate(db, broken)
            # всё до сломанной миграции применилось, сама она — нет
            assert await get_version(db) == LATEST_VERSION
            assert "half_done" not in await tables(db)
            # после исправления миграция применяется с того же места
            fixed = MIGRATIONS + (
                Migration(LATEST_VERSION + 1, "fixed", ("CREATE TABLE half_done (id INTEGER)",)),
            )
            assert await migrate(db, fixed) == [LATEST_VERSION + 1]
    run(scenario())


def test_duplicate_columns_fail_outside_baseline():
    extra = MIGRATIONS + (
        Migration(LATEST_VERSION + 1, "dup", ("ALTER TABLE users ADD COLUMN llm_model TEXT",)),
    )

    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            with pytest.raises(Exception, match="duplicate column"):
                await migrate(db, extra)
    run(scenario())


def test_versions_must_increase():
    bad = (Migration(2, "b", ()), Migration(1, "a", ()))

    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            with pytest.raises(ValueError):
                await migrate(db, bad)
    run(scenario())


def test_init_db_on_file(tmp_path):
    from database import init_db
    from database.db import db_connect

    path = str(tmp_path / "sub" / "bot.db")

    async def scenario():
        await init_db(path)
        await init_db(path)  # повторный старт бота ничего не ломает
        async with db_connect(path) as db:
            assert await get_version(db) == LATEST_VERSION
    run(scenario())
