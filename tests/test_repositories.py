"""Репозитории на SQLite в памяти, с фиксированным «сейчас»."""
import asyncio

import aiosqlite
import pytest

from database.migrations import migrate
from database.repositories import CostsRepository, UsersRepository, WbStatsRepository

# 10.10.2026 13:00 по Алматы = 08:00 UTC
NOW = "2026-10-10 08:00:00"


def with_db(test):
    """Каждый тест получает свежую базу в памяти со всеми миграциями."""
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await migrate(db)
            await test(db)
    asyncio.run(scenario())


async def insert_at(db, table, created_at, **values):
    values["created_at"] = created_at
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    await db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    await db.commit()


# ---------- пользователи ----------
def test_users_add_remove_and_list():
    async def t(db):
        repo = UsersRepository(db)
        assert not await repo.exists(10)
        await repo.add(10)
        await repo.add(10)  # повторный /start не дублирует
        await repo.add(20)
        assert await repo.exists(10)
        assert await repo.list_all() == [(10, "deepseek"), (20, "deepseek")]
        assert sorted(await repo.all_ids()) == [10, 20]
        await repo.remove(10)
        assert await repo.all_ids() == [20]
    with_db(t)


def test_llm_model_default_and_upsert():
    async def t(db):
        repo = UsersRepository(db)
        assert await repo.get_llm_model(5) == "deepseek"
        # у пользователя нет строки (как у админа) — модель всё равно сохраняется
        await repo.set_llm_model(5, "gemini")
        assert await repo.get_llm_model(5) == "gemini"
        await repo.set_llm_model(5, "openai")
        assert await repo.get_llm_model(5) == "openai"
        assert await repo.list_all() == [(5, "openai")]
    with_db(t)


def test_infographic_style():
    async def t(db):
        repo = UsersRepository(db)
        await repo.add(7)
        assert await repo.get_style(7) == "default"
        await repo.set_style(7, "dark")
        assert await repo.get_style(7) == "dark"
        assert await repo.get_style(999) == "default"
    with_db(t)


# ---------- расходы ----------
def test_costs_summary_by_period():
    async def t(db):
        repo = CostsRepository(db)
        await insert_at(db, "costs", "2026-10-10 05:00:00", operation="card", provider="deepseek", usd=0.10)
        await insert_at(db, "costs", "2026-10-10 06:00:00", operation="card", provider="deepseek", usd=0.05)
        await insert_at(db, "costs", "2026-10-10 07:00:00", operation="image", provider="gemini", usd=0.20)
        await insert_at(db, "costs", "2026-10-09 12:00:00", operation="card", provider="openai", usd=1.00)
        await insert_at(db, "costs", "2026-09-01 12:00:00", operation="card", provider="openai", usd=5.00)

        summary = dict(await repo.summary(now=NOW))
        today = {r.provider: (r.calls, round(r.usd, 2)) for r in summary["Сегодня"]}
        assert today == {"deepseek": (2, 0.15), "gemini": (1, 0.20)}
        assert [(r.provider, r.calls) for r in summary["Вчера"]] == [("openai", 1)]
        assert sum(r.usd for r in summary["7 дней"]) == pytest.approx(1.35)
        assert sum(r.usd for r in summary["30 дней"]) == pytest.approx(1.35)
        assert sum(r.usd for r in summary["Всего"]) == pytest.approx(6.35)
    with_db(t)


def test_costs_add_and_top_operations():
    async def t(db):
        repo = CostsRepository(db)
        await repo.add(1, "card", provider="deepseek", input_tokens=100, output_tokens=50, usd=0.01)
        await repo.add(1, "image", provider="gemini", usd=0.30, gemini_images=1)
        await repo.add(1, "image", provider="gemini", usd=0.30, gemini_images=1)
        top = await repo.top_operations(days=7, limit=5)
        assert [(o.operation, o.calls) for o in top] == [("image", 2), ("card", 1)]
        assert top[0].usd == pytest.approx(0.60)
        assert await repo.top_operations(limit=1) == top[:1]
    with_db(t)


# ---------- статистика WB ----------
def test_work_window_is_almaty_09_30_to_18_30():
    async def t(db):
        # время в UTC; по Алматы это +5 часов
        for article, utc in [
            ("before", "2026-10-10 04:29:59"),   # 09:29:59 — ещё не рабочее время
            ("start", "2026-10-10 04:30:00"),    # 09:30
            ("midday", "2026-10-10 08:00:00"),   # 13:00
            ("end", "2026-10-10 13:30:00"),      # 18:30
            ("after", "2026-10-10 13:31:00"),    # 18:31
            ("yesterday", "2026-10-09 08:00:00"),
        ]:
            await insert_at(db, "wb_created_cards", utc, article=article, nm_id=1, title=article)
        repo = WbStatsRepository(db)
        window = [r["article"] for r in await repo.created_in_work_window(now=NOW)]
        assert window == ["start", "midday", "end"]
        assert await repo.count_created_in_work_window(now=NOW) == 3
        since = [r["article"] for r in await repo.created_since_work_start(now=NOW)]
        assert since == ["start", "midday", "end", "after"]
    with_db(t)


def test_almaty_day_differs_from_utc_day():
    """В 23:00 UTC в Алматы уже следующий день — окно считается по алматинскому."""
    async def t(db):
        await insert_at(db, "wb_created_cards", "2026-10-10 05:00:00", article="utc_day", nm_id=1)
        await insert_at(db, "wb_created_cards", "2026-10-11 05:00:00", article="almaty_day", nm_id=2)
        repo = WbStatsRepository(db)
        rows = await repo.created_in_work_window(now="2026-10-10 23:00:00")
        assert [r["article"] for r in rows] == ["almaty_day"]
    with_db(t)


def test_failures_and_tnved_misses():
    async def t(db):
        repo = WbStatsRepository(db)
        hw = {"ram_free_gb": 12.5, "gpu_util_pct": 80}
        await repo.add_failure(1, "A-1", "Мышь", "x" * 1000, subject="Мыши", hw=hw)
        await repo.add_tnved_miss(1, "A-2", 55, "Кабели", "Кабель " * 100)
        await repo.add_created(1, "A-3", 777, "Колонка", "Колонки", photos_count=5,
                               elapsed_sec=240.0, hw=hw)
        await db.execute("UPDATE wb_create_failures SET created_at = ?", ("2026-10-10 06:00:00",))
        await db.execute("UPDATE wb_tnved_misses SET created_at = ?", ("2026-10-10 06:00:00",))
        await db.execute("UPDATE wb_created_cards SET created_at = ?", ("2026-10-10 06:00:00",))
        await db.commit()

        [fail] = await repo.failures_in_work_window(now=NOW)
        assert fail["article"] == "A-1" and fail["subject"] == "Мыши"
        assert len(fail["error"]) == 500
        assert fail["ram_free_gb"] == 12.5 and fail["gpu_used_mb"] is None

        [miss] = await repo.tnved_misses_in_work_window(now=NOW)
        assert miss["subject_id"] == 55 and len(miss["product_name"]) == 200

        [card] = await repo.created_in_work_window(now=NOW)
        assert (card["nm_id"], card["photos_count"], card["gpu_util_pct"]) == (777, 5, 80)
    with_db(t)
