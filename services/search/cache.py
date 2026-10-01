"""
Кэш сырых ответов поисковых движков (SearXNG) — бесплатная мера снижения
нагрузки на публичные движки (14.07.2026). Одинаковые/похожие запросы
(повторные попытки после ошибки, соседние товары той же линейки, ретраи
батчей) сейчас каждый раз бьют по Brave/Google/Startpage заново — это и
приближает дневной лимit. TTL 24ч: характеристики/фото товара за день не
меняются, а повторный батч в тот же день — обычное дело.
"""
import hashlib
import json
import logging
import time

from database.db import db_connect

log = logging.getLogger(__name__)

_TTL_SECONDS = 24 * 3600
_memory_cache: dict[str, tuple[float, str]] = {}


def make_key(*parts: str) -> str:
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get_cached(key: str):
    now = time.time()
    hit = _memory_cache.get(key)
    if hit and now - hit[0] < _TTL_SECONDS:
        return json.loads(hit[1])

    async with db_connect() as db:
        cur = await db.execute(
            "SELECT payload, created_at FROM search_cache WHERE cache_key = ?", (key,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    payload, created_at = row
    try:
        age = time.time() - time.mktime(time.strptime(created_at, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    if age > _TTL_SECONDS:
        return None
    _memory_cache[key] = (now, payload)
    return json.loads(payload)


async def set_cached(key: str, value) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    _memory_cache[key] = (time.time(), payload)
    async with db_connect() as db:
        await db.execute(
            """INSERT INTO search_cache (cache_key, payload, created_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(cache_key) DO UPDATE SET
                   payload = excluded.payload, created_at = excluded.created_at""",
            (key, payload),
        )
        await db.commit()
