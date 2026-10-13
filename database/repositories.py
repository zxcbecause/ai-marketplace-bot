"""Репозитории: весь SQL бота по пользователям, расходам и статистике WB.

Обработчики и сервисы не пишут запросы сами, а вызывают методы репозиториев.
Репозиторий получает уже открытое соединение, поэтому его легко проверить на
базе в памяти (`aiosqlite.connect(":memory:")`), а в боте он работает через
`db_connect()` со всеми PRAGMA.
"""
from dataclasses import dataclass

DEFAULT_LLM = "deepseek"
DEFAULT_STYLE = "default"

# Рабочее окно отчётов: 09:30–18:30 по Алматы (UTC+5, без летнего времени).
# В базе created_at хранится в UTC, поэтому обе стороны сдвигаем на +5 часов.
# `?` — момент «сейчас»: в боте 'now', в тестах любая фиксированная дата.
_LOCAL_CREATED = "datetime(created_at, '+5 hours')"
_DAY_START = "datetime(?, '+5 hours', 'start of day', '+9 hours', '+30 minutes')"
_DAY_END = "datetime(?, '+5 hours', 'start of day', '+18 hours', '+30 minutes')"
WORK_WINDOW = f"{_LOCAL_CREATED} >= {_DAY_START} AND {_LOCAL_CREATED} <= {_DAY_END}"
SINCE_WORK_START = f"{_LOCAL_CREATED} >= {_DAY_START}"

# Периоды для /costs: (подпись, условие, сколько раз подставить «сейчас»)
COST_PERIODS = (
    ("Сегодня", "date(created_at) = date(?)", 1),
    ("Вчера", "date(created_at) = date(?, '-1 day')", 1),
    ("7 дней", "created_at >= datetime(?, '-7 days')", 1),
    ("30 дней", "created_at >= datetime(?, '-30 days')", 1),
    ("Всего", "1=1", 0),
)

_HW_KEYS = ("ram_free_gb", "gpu_used_mb", "gpu_total_mb", "gpu_util_pct")


class UsersRepository:
    def __init__(self, db):
        self.db = db

    async def exists(self, user_id: int) -> bool:
        cur = await self.db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None

    async def add(self, user_id: int) -> None:
        """Добавляет пользователя; если он уже есть, ничего не меняет."""
        await self.db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await self.db.commit()

    async def remove(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await self.db.commit()

    async def list_all(self) -> list[tuple[int, str]]:
        """(user_id, llm_model) в порядке добавления."""
        cur = await self.db.execute(
            "SELECT user_id, llm_model FROM users ORDER BY created_at, rowid"
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    async def all_ids(self) -> list[int]:
        cur = await self.db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]

    async def get_llm_model(self, user_id: int) -> str:
        cur = await self.db.execute("SELECT llm_model FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else DEFAULT_LLM

    async def set_llm_model(self, user_id: int, model: str) -> None:
        # Upsert: у админа может не быть строки в users (доступ ему даёт ADMIN_ID),
        # и простой UPDATE молча ничего бы не сделал.
        await self.db.execute(
            "INSERT INTO users (user_id, llm_model) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET llm_model = excluded.llm_model",
            (user_id, model),
        )
        await self.db.commit()

    async def get_style(self, user_id: int) -> str:
        cur = await self.db.execute(
            "SELECT infographic_style FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row and row[0] else DEFAULT_STYLE

    async def set_style(self, user_id: int, style: str) -> None:
        await self.db.execute(
            "UPDATE users SET infographic_style = ? WHERE user_id = ?", (style, user_id)
        )
        await self.db.commit()


@dataclass(frozen=True)
class ProviderTotal:
    provider: str
    calls: int
    usd: float


@dataclass(frozen=True)
class OperationTotal:
    operation: str
    calls: int
    usd: float


class CostsRepository:
    def __init__(self, db):
        self.db = db

    async def add(self, user_id: int, operation: str, provider: str = "",
                  input_tokens: int = 0, output_tokens: int = 0, usd: float = 0.0,
                  exa_requests: int = 0, gemini_images: int = 0) -> None:
        await self.db.execute(
            """INSERT INTO costs
               (user_id, operation, provider, input_tokens, output_tokens, usd,
                exa_requests, gemini_images)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, operation, provider, input_tokens, output_tokens, usd,
             exa_requests, gemini_images),
        )
        await self.db.commit()

    async def by_provider(self, condition: str, params: tuple = ()) -> list[ProviderTotal]:
        cur = await self.db.execute(
            f"SELECT provider, COUNT(*), COALESCE(SUM(usd), 0) FROM costs "
            f"WHERE {condition} GROUP BY provider ORDER BY provider",
            params,
        )
        return [ProviderTotal(r[0] or "", r[1], float(r[2])) for r in await cur.fetchall()]

    async def summary(self, now: str = "now") -> list[tuple[str, list[ProviderTotal]]]:
        """Траты по провайдерам за каждый период из COST_PERIODS."""
        return [
            (label, await self.by_provider(cond, (now,) * n_params))
            for label, cond, n_params in COST_PERIODS
        ]

    async def top_operations(self, days: int = 7, limit: int = 5,
                             now: str = "now") -> list[OperationTotal]:
        cur = await self.db.execute(
            "SELECT operation, COUNT(*), COALESCE(SUM(usd), 0) FROM costs "
            "WHERE created_at >= datetime(?, ?) "
            "GROUP BY operation ORDER BY SUM(usd) DESC LIMIT ?",
            (now, f"-{int(days)} days", limit),
        )
        return [OperationTotal(r[0] or "", r[1], float(r[2])) for r in await cur.fetchall()]


class WbStatsRepository:
    """Созданные карточки WB, провалы и карточки без ТН ВЭД для вечернего отчёта."""

    def __init__(self, db):
        self.db = db

    # ---- запись ----
    async def add_created(self, user_id: int, article: str, nm_id: int, title: str,
                          subject: str, photos_count: int = 0,
                          elapsed_sec: float | None = None, hw: dict | None = None) -> None:
        hw = hw or {}
        await self.db.execute(
            """INSERT INTO wb_created_cards
               (user_id, article, nm_id, title, subject, photos_count, elapsed_sec,
                ram_free_gb, gpu_used_mb, gpu_total_mb, gpu_util_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, article, nm_id, title, subject, photos_count, elapsed_sec,
             *(hw.get(k) for k in _HW_KEYS)),
        )
        await self.db.commit()

    async def add_failure(self, user_id: int, article: str, name: str, error: str,
                          subject: str = "", hw: dict | None = None) -> None:
        hw = hw or {}
        await self.db.execute(
            """INSERT INTO wb_create_failures
               (user_id, article, name, error, subject,
                ram_free_gb, gpu_used_mb, gpu_total_mb, gpu_util_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, article, name, str(error)[:500], subject,
             *(hw.get(k) for k in _HW_KEYS)),
        )
        await self.db.commit()

    async def add_tnved_miss(self, user_id: int, article: str, subject_id: int,
                             subject_name: str, product_name: str) -> None:
        await self.db.execute(
            """INSERT INTO wb_tnved_misses
               (user_id, article, subject_id, subject_name, product_name)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, article, subject_id, subject_name, (product_name or "")[:200]),
        )
        await self.db.commit()

    # ---- чтение за рабочий день ----
    async def _rows(self, columns: str, table: str, condition: str,
                    now: str, n_params: int) -> list[dict]:
        cur = await self.db.execute(
            f"SELECT {columns} FROM {table} WHERE {condition} ORDER BY created_at, id",
            (now,) * n_params,
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in await cur.fetchall()]

    async def created_in_work_window(self, now: str = "now") -> list[dict]:
        return await self._rows(
            "article, nm_id, title, subject, photos_count, elapsed_sec, "
            "ram_free_gb, gpu_used_mb, gpu_total_mb, gpu_util_pct, created_at",
            "wb_created_cards", WORK_WINDOW, now, 2)

    async def created_since_work_start(self, now: str = "now") -> list[dict]:
        """Без верхней границы: вечерний отчёт уходит до конца рабочего окна."""
        return await self._rows(
            "article, nm_id, title, subject, photos_count, elapsed_sec, created_at",
            "wb_created_cards", SINCE_WORK_START, now, 1)

    async def count_created_in_work_window(self, now: str = "now") -> int:
        cur = await self.db.execute(
            f"SELECT COUNT(*) FROM wb_created_cards WHERE {WORK_WINDOW}", (now, now)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def failures_in_work_window(self, now: str = "now") -> list[dict]:
        return await self._rows(
            "article, name, error, subject, ram_free_gb, gpu_used_mb, "
            "gpu_total_mb, gpu_util_pct, created_at",
            "wb_create_failures", WORK_WINDOW, now, 2)

    async def tnved_misses_in_work_window(self, now: str = "now") -> list[dict]:
        return await self._rows(
            "article, subject_id, subject_name, product_name",
            "wb_tnved_misses", WORK_WINDOW, now, 2)


class SearchCacheRepository:
    """Кэш ответов поисковых движков. Возраст записи считает сама SQLite:
    created_at и datetime('now') оба в UTC, часовой пояс ПК не влияет."""

    def __init__(self, db):
        self.db = db

    async def get(self, key: str, ttl_seconds: int, now: str = "now") -> tuple[str, int] | None:
        """(payload, сколько секунд записи ещё жить) или None, если записи нет или она устарела."""
        cur = await self.db.execute(
            """SELECT payload,
                      ? - (CAST(strftime('%s', ?) AS INTEGER) - CAST(strftime('%s', created_at) AS INTEGER))
               FROM search_cache
               WHERE cache_key = ? AND created_at > datetime(?, ?)""",
            (ttl_seconds, now, key, now, f"-{int(ttl_seconds)} seconds"),
        )
        row = await cur.fetchone()
        return (row[0], int(row[1])) if row else None

    async def put(self, key: str, payload: str, now: str = "now") -> None:
        await self.db.execute(
            """INSERT INTO search_cache (cache_key, payload, created_at)
               VALUES (?, ?, datetime(?))
               ON CONFLICT(cache_key) DO UPDATE SET
                   payload = excluded.payload, created_at = excluded.created_at""",
            (key, payload, now),
        )
        await self.db.commit()

    async def purge_expired(self, ttl_seconds: int, now: str = "now") -> int:
        """Удаляет устаревшие записи, возвращает их количество."""
        cur = await self.db.execute(
            "DELETE FROM search_cache WHERE created_at <= datetime(?, ?)",
            (now, f"-{int(ttl_seconds)} seconds"),
        )
        await self.db.commit()
        return cur.rowcount

    async def count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) FROM search_cache")
        return (await cur.fetchone())[0]


class TnvedRepository:
    """Своя библиотека кодов ТН ВЭД: по профилям Ozon, по subjectID WB и кандидаты."""

    def __init__(self, db):
        self.db = db

    async def get_ozon(self, category_key: str) -> str | None:
        cur = await self.db.execute(
            "SELECT tnved FROM tnved_codes WHERE category_key = ?", (category_key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_ozon(self, category_key: str, tnved: str, note: str = "") -> None:
        await self.db.execute(
            """INSERT INTO tnved_codes (category_key, tnved, note, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(category_key) DO UPDATE SET
                   tnved = excluded.tnved, note = excluded.note,
                   updated_at = excluded.updated_at""",
            (category_key, tnved, note),
        )
        await self.db.commit()

    async def seed_ozon(self, codes: dict[str, str], note: str) -> int:
        """Добавляет коды для категорий, которых ещё нет; существующие не трогает.
        Возвращает число добавленных."""
        before = self.db.total_changes
        await self.db.executemany(
            "INSERT OR IGNORE INTO tnved_codes (category_key, tnved, note) VALUES (?, ?, ?)",
            [(key, code, note) for key, code in codes.items()],
        )
        await self.db.commit()
        return self.db.total_changes - before

    async def get_wb(self, subject_id: int) -> str | None:
        cur = await self.db.execute(
            "SELECT tnved FROM wb_tnved_codes WHERE subject_id = ?", (subject_id,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_wb(self, subject_id: int, subject_name: str, tnved: str, note: str = "") -> None:
        await self.db.execute(
            """INSERT INTO wb_tnved_codes (subject_id, subject_name, tnved, note, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(subject_id) DO UPDATE SET
                   subject_name = excluded.subject_name, tnved = excluded.tnved,
                   note = excluded.note, updated_at = excluded.updated_at""",
            (subject_id, subject_name, tnved, note),
        )
        await self.db.commit()

    async def get_wb_candidates(self, subject_id: int) -> list[dict]:
        cur = await self.db.execute(
            "SELECT tnved, description FROM wb_tnved_candidates "
            "WHERE subject_id = ? ORDER BY tnved",
            (subject_id,),
        )
        return [{"tnved": r[0], "description": r[1]} for r in await cur.fetchall()]

    async def set_wb_candidates(self, subject_id: int, subject_name: str,
                                candidates: list[tuple[str, str]], source: str = "") -> None:
        """Апсерт по (subject_id, tnved): повторный сид обновляет описания, не плодит дубли."""
        await self.db.executemany(
            """INSERT INTO wb_tnved_candidates
                   (subject_id, tnved, description, subject_name, source, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(subject_id, tnved) DO UPDATE SET
                   description = excluded.description,
                   subject_name = excluded.subject_name,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            [(subject_id, code, desc, subject_name, source) for code, desc in candidates],
        )
        await self.db.commit()


class DimsHistoryRepository:
    """Реальные габариты упаковки, найденные для карточек, — по категориям WB."""

    def __init__(self, db):
        self.db = db

    async def add(self, subject_id: int, length: float, width: float,
                  height: float, weight: float, source: str = "") -> None:
        await self.db.execute(
            """INSERT INTO wb_dims_history (subject_id, length, width, height, weight, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (subject_id, length, width, height, weight, source),
        )
        await self.db.commit()

    async def recent(self, subject_id: int, limit: int = 50) -> list[tuple[float, float, float, float]]:
        """Последние `limit` образцов категории, новые первыми."""
        cur = await self.db.execute(
            """SELECT length, width, height, weight FROM wb_dims_history
               WHERE subject_id = ? ORDER BY id DESC LIMIT ?""",
            (subject_id, limit),
        )
        return [tuple(r) for r in await cur.fetchall()]
