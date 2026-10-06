# -*- coding: utf-8 -*-
"""Пауза между повторами запросов к WB API.

WB при превышении лимита отвечает 429 и обычно сообщает, сколько ждать:
заголовки X-Ratelimit-Retry / X-Ratelimit-Reset (секунды) или стандартный Retry-After.
Если заголовков нет — экспоненциальная пауза с потолком.
"""

RETRY_HEADERS = ("X-Ratelimit-Retry", "X-Ratelimit-Reset", "Retry-After")


def backoff_seconds(attempt: int, headers=None, base: float = 6.0, cap: float = 120.0) -> float:
    """Сколько ждать перед повтором номер `attempt` (с нуля).

    Значение из заголовка WB важнее расчётного, но тоже ограничено `cap`,
    чтобы скрипт не «замолкал» на долгие минуты из-за одного ответа.
    """
    for name in RETRY_HEADERS:
        value = (headers or {}).get(name)
        if value is None:
            continue
        try:
            seconds = float(str(value).strip())
        except ValueError:
            continue
        if seconds >= 0:
            return min(max(seconds, 1.0), cap)
    return min(base * (2 ** attempt), cap)
