"""ТН ВЭД: извлечение кодов со страниц классификаторов и библиотека кодов в SQLite."""
import asyncio

import aiosqlite
import pytest

from database.migrations import migrate
from database.repositories import DimsHistoryRepository, TnvedRepository
from services.tnved import _extract_tnved_codes

NBSP = " "


# ---------- извлечение кодов ----------
@pytest.mark.parametrize("text", [
    "Код ТН ВЭД 8518109600 — громкоговорители",       # слитно
    "Код ТН ВЭД 8518 10 960 0 — громкоговорители",    # официальная запись 4-2-3-1
    "Код ТН ВЭД 8518 10 96 00 — громкоговорители",    # 4-2-2-2
    "Код ТН ВЭД 8518 10 9600 — громкоговорители",     # 4-2-4
    f"Код ТН ВЭД 8518{NBSP}10{NBSP}960{NBSP}0",        # неразрывные пробелы с сайта
])
def test_code_formats_in_text(text):
    assert _extract_tnved_codes("https://example.com/list", "", text) == {"8518109600"}


def test_code_in_title_and_url():
    codes = _extract_tnved_codes(
        "https://tws.by/tnved/8471300000/", "8517 62 000 9 — аппаратура передачи данных", "")
    assert codes == {"8471300000", "8517620009"}


@pytest.mark.parametrize("text", [
    "Обновлено 2026-10-13 15:20",          # дата через дефисы
    "Обновлено 2026-10-13 15:20:31",
    "Телефон 8 701 234 56 78",             # телефон
    "Артикул 0012345678",                  # группа 00 не существует
    "Номер 9912345678",                    # группы 98-99 нет
    "Код 85181096001",                     # 11 цифр — не код
    "Цена 1 234 567 890 тг",
])
def test_not_a_code(text):
    assert _extract_tnved_codes("https://example.com/", "", text) == set()


def test_several_codes_on_one_page():
    text = "Рюкзаки: 4202 92 110 0 (пластмасса) или 4202 91 100 0 (кожа); см. также 4202929800"
    assert _extract_tnved_codes("https://example.com/", "", text) == {
        "4202921100", "4202911000", "4202929800"}


# ---------- библиотека кодов ----------
def with_db(test):
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await migrate(db)
            await test(db)
    asyncio.run(scenario())


def test_ozon_codes_and_seed_does_not_overwrite():
    async def t(db):
        repo = TnvedRepository(db)
        assert await repo.get_ozon("mouse") is None
        await repo.set_ozon("mouse", "8471607000", note="поправлено вручную")
        added = await repo.seed_ozon({"mouse": "0000000000", "speaker": "8518220009"}, note="сид")
        assert added == 1
        assert await repo.get_ozon("mouse") == "8471607000"  # ручная правка сильнее сида
        assert await repo.get_ozon("speaker") == "8518220009"
        assert await repo.seed_ozon({"speaker": "1"}, note="сид") == 0
        await repo.set_ozon("speaker", "8518210000")
        assert await repo.get_ozon("speaker") == "8518210000"
    with_db(t)


def test_wb_codes_and_candidates_upsert():
    async def t(db):
        repo = TnvedRepository(db)
        await repo.set_wb(5, "Мыши", "8471607000", note="из справочника WB")
        await repo.set_wb(5, "Мыши компьютерные", "8471607001")
        assert await repo.get_wb(5) == "8471607001"
        assert await repo.get_wb(6) is None

        await repo.set_wb_candidates(9, "Рюкзаки", [("4202929800", "прочие"),
                                                     ("4202921100", "пластмасса")], source="web")
        await repo.set_wb_candidates(9, "Рюкзаки", [("4202921100", "из пластмассы")], source="web")
        assert await repo.get_wb_candidates(9) == [
            {"tnved": "4202921100", "description": "из пластмассы"},
            {"tnved": "4202929800", "description": "прочие"},
        ]
        await repo.set_wb_candidates(9, "Рюкзаки", [])  # пустой список ничего не ломает
        assert len(await repo.get_wb_candidates(9)) == 2
    with_db(t)


# ---------- история габаритов ----------
def test_dims_history_returns_latest_samples_first():
    async def t(db):
        repo = DimsHistoryRepository(db)
        for i in range(1, 6):
            await repo.add(100, 10 * i, 5, 3, 0.1 * i, source="test")
        await repo.add(200, 99, 99, 99, 9.9)
        assert await repo.recent(100, limit=2) == [(50, 5, 3, 0.5), (40, 5, 3, 0.4)]
        assert len(await repo.recent(100)) == 5
        assert await repo.recent(300) == []
    with_db(t)


# ---------- медиана габаритов (services/dims_history.py) ----------
def test_median_dims():
    from services.dims_history import median_dims
    assert median_dims([]) is None
    assert median_dims([(10, 5, 3, 0.5)]) == (10, 5, 3, 0.5)
    assert median_dims([(10, 5, 3, 0.5), (30, 9, 5, 1.5), (20, 7, 4, 1.0)]) == (20, 7, 4, 1.0)


def test_median_uses_only_recent_samples(tmp_path, monkeypatch):
    import services.dims_history as dims
    from database.db import db_connect, init_db

    db_file = str(tmp_path / "bot.db")
    monkeypatch.setattr(dims, "HISTORY_WINDOW", 3)
    monkeypatch.setattr(dims, "db_connect", lambda: db_connect(db_file))

    async def t():
        await init_db(db_file)
        for _ in range(5):  # старые образцы: маленькие коробки
            await dims.record_dims(1, 10, 10, 10, 0.5)
        for size in (40, 50, 60):  # свежие: ассортимент сменился на крупный
            await dims.record_dims(1, size, size, size, 3.0)
        assert await dims.get_dims_median(1) == (50, 50, 50, 3.0)
        assert await dims.get_dims_median(2) is None
    asyncio.run(t())


def test_database_error_falls_back_to_default(monkeypatch):
    import services.dims_history as dims

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dims, "db_connect", broken)

    async def t():
        assert await dims.get_dims_median(1) is None   # не исключение
        await dims.record_dims(1, 10, 10, 10, 1.0)     # тоже не падает
    asyncio.run(t())
