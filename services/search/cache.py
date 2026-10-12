"""
Кэш сырых ответов поисковых движков (SearXNG) — бесплатная мера снижения
нагрузки на публичные движки (14.07.2026). Одинаковые/похожие запросы
(повторные попытки после ошибки, соседние товары той же линейки, ретраи
батчей) сейчас каждый раз бьют по Brave/Google/Startpage заново — это и
приближает дневной лимит. TTL 24ч: характеристики/фото товара за день не
меняются, а повторный батч в тот же день — обычное дело.

Два уровня: словарь в памяти процесса (ограничен по размеру, LRU) и таблица
search_cache в SQLite. Срок жизни записи в памяти не продлевается при чтении:
запись, поднятая из базы, живёт ровно столько, сколько ей осталось в базе.
Кэш — оптимизация, поэтому его сбой (например, база занята) не ломает поиск.
"""
import hashlib
import json
import logging
import time
from collections import OrderedDict

from database.db import db_connect
from database.repositories import SearchCacheRepository

log = logging.getLogger(__name__)

_TTL_SECONDS = 24 * 3600
_MEMORY_MAX_ITEMS = 500
_PURGE_EVERY_SECONDS = 3600

# key -> (момент истечения по time.monotonic(), payload)
_memory_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_last_purge: float | None = None  # None — ещё не чистили в этом процессе


def make_key(*parts: str) -> str:
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _remember(key: str, payload: str, ttl_left: float) -> None:
    _memory_cache[key] = (time.monotonic() + ttl_left, payload)
    _memory_cache.move_to_end(key)
    while len(_memory_cache) > _MEMORY_MAX_ITEMS:
        _memory_cache.popitem(last=False)


async def get_cached(key: str):
    hit = _memory_cache.get(key)
    if hit:
        expires_at, payload = hit
        if time.monotonic() < expires_at:
            _memory_cache.move_to_end(key)
            return json.loads(payload)
        del _memory_cache[key]

    try:
        async with db_connect() as db:
            row = await SearchCacheRepository(db).get(key, _TTL_SECONDS)
    except Exception as e:
        log.warning(f"search_cache: чтение не удалось, идём в поиск: {e}")
        return None
    if row is None:
        return None
    payload, ttl_left = row
    _remember(key, payload, ttl_left)
    return json.loads(payload)


async def set_cached(key: str, value) -> None:
    global _last_purge
    payload = json.dumps(value, ensure_ascii=False)
    _remember(key, payload, _TTL_SECONDS)
    try:
        async with db_connect() as db:
            repo = SearchCacheRepository(db)
            await repo.put(key, payload)
            # раз в час чистим устаревшее, иначе таблица растёт бесконечно
            if _last_purge is None or time.monotonic() - _last_purge > _PURGE_EVERY_SECONDS:
                _last_purge = time.monotonic()
                removed = await repo.purge_expired(_TTL_SECONDS)
                if removed:
                    log.info(f"search_cache: удалено устаревших записей: {removed}")
    except Exception as e:
        log.warning(f"search_cache: запись не удалась: {e}")
