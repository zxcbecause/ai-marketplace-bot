"""Кэш поисковых ответов: репозиторий на SQLite в памяти и двухуровневый кэш поверх файла."""
import asyncio
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest

from database.migrations import migrate
from database.repositories import SearchCacheRepository

DAY = 24 * 3600
NOW = "2026-10-12 12:00:00"


def with_db(test):
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await migrate(db)
            await test(SearchCacheRepository(db))
    asyncio.run(scenario())


# ---------- репозиторий ----------
def test_fresh_entry_is_returned_with_remaining_ttl():
    async def t(repo):
        await repo.put("k", '{"a": 1}', now="2026-10-12 09:00:00")
        payload, ttl_left = await repo.get("k", DAY, now=NOW)
        assert payload == '{"a": 1}'
        assert ttl_left == DAY - 3 * 3600
    with_db(t)


def test_expired_entry_is_a_miss():
    async def t(repo):
        await repo.put("old", "[]", now="2026-10-11 11:59:59")
        await repo.put("edge", "[]", now="2026-10-11 12:00:00")
        await repo.put("fresh", "[]", now="2026-10-11 12:00:01")
        assert await repo.get("old", DAY, now=NOW) is None
        assert await repo.get("edge", DAY, now=NOW) is None  # ровно сутки — уже устарела
        assert await repo.get("fresh", DAY, now=NOW) is not None
        assert await repo.get("missing", DAY, now=NOW) is None
    with_db(t)


def test_put_overwrites_and_renews():
    async def t(repo):
        await repo.put("k", "1", now="2026-10-10 00:00:00")
        await repo.put("k", "2", now="2026-10-12 11:00:00")
        assert await repo.get("k", DAY, now=NOW) == ("2", DAY - 3600)
        assert await repo.count() == 1
    with_db(t)


def test_purge_removes_only_expired():
    async def t(repo):
        await repo.put("a", "[]", now="2026-10-01 00:00:00")
        await repo.put("b", "[]", now="2026-10-11 10:00:00")
        await repo.put("c", "[]", now="2026-10-12 11:00:00")
        assert await repo.purge_expired(DAY, now=NOW) == 2
        assert await repo.count() == 1
        assert await repo.get("c", DAY, now=NOW) is not None
    with_db(t)


# ---------- двухуровневый кэш (services/search/cache.py) ----------
@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Модуль кэша на временном файле базы. Грузим файл напрямую, без
    services.search.__init__, который тянет тяжёлые зависимости поиска."""
    path = Path(__file__).resolve().parent.parent / "services" / "search" / "cache.py"
    spec = importlib.util.spec_from_file_location("search_cache_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db_file = str(tmp_path / "bot.db")
    from database.db import db_connect, init_db
    asyncio.run(init_db(db_file))

    @asynccontextmanager
    async def connect():
        async with db_connect(db_file) as db:
            yield db

    monkeypatch.setattr(module, "db_connect", connect)
    module.db_file = db_file
    return module


def test_roundtrip_through_memory_and_database(cache):
    async def t():
        key = cache.make_key("searxng_raw", "мышь logitech")
        assert await cache.get_cached(key) is None
        await cache.set_cached(key, [{"url": "https://example.com", "title": "Мышь"}])
        assert await cache.get_cached(key) == [{"url": "https://example.com", "title": "Мышь"}]
        # после перезапуска процесса (пустая память) значение достаётся из базы
        cache._memory_cache.clear()
        assert await cache.get_cached(key) == [{"url": "https://example.com", "title": "Мышь"}]
        assert key in cache._memory_cache
    asyncio.run(t())


def test_entry_from_database_keeps_its_remaining_ttl(cache):
    async def t():
        async with aiosqlite.connect(cache.db_file) as db:
            await SearchCacheRepository(db).put("k", '"v"', now="now")
            await db.execute("UPDATE search_cache SET created_at = datetime('now', '-23 hours')")
            await db.commit()
        assert await cache.get_cached("k") == "v"
        expires_at, _ = cache._memory_cache["k"]
        left = expires_at - cache.time.monotonic()
        assert 3500 < left <= 3600  # осталось около часа, а не новые сутки
    asyncio.run(t())


def test_memory_layer_is_bounded(cache, monkeypatch):
    monkeypatch.setattr(cache, "_MEMORY_MAX_ITEMS", 3)

    async def t():
        for i in range(5):
            await cache.set_cached(f"k{i}", i)
        assert list(cache._memory_cache) == ["k2", "k3", "k4"]
        # вытесненное из памяти всё равно есть в базе
        assert await cache.get_cached("k0") == 0
    asyncio.run(t())


def test_database_failure_does_not_break_search(cache, monkeypatch):
    @asynccontextmanager
    async def broken():
        raise RuntimeError("database is locked")
        yield  # pragma: no cover

    monkeypatch.setattr(cache, "db_connect", broken)

    async def t():
        assert await cache.get_cached("nope") is None
        await cache.set_cached("k", [1, 2])  # не падает
        assert await cache.get_cached("k") == [1, 2]  # из памяти
    asyncio.run(t())


def test_expired_rows_are_purged_on_write(cache):
    async def t():
        async with aiosqlite.connect(cache.db_file) as db:
            await SearchCacheRepository(db).put("stale", "1", now="2026-01-01 00:00:00")
        await cache.set_cached("new", 2)
        async with aiosqlite.connect(cache.db_file) as db:
            assert await SearchCacheRepository(db).count() == 1
    asyncio.run(t())
