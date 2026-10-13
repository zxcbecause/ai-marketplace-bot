"""Самообучающиеся габариты упаковки по категориям WB.

Когда веб-поиск находит полный набор габаритов товара (длина, ширина, высота,
вес из одного источника), он запоминается как образец категории. Если для
следующего товара той же категории габариты не нашлись, берётся медиана
последних образцов, и только при пустой истории — грубый статичный дефолт
(см. _build_dimensions в services/wb_create.py).

История — подсказка, а не обязательный шаг: ошибки базы здесь логируются и
не прерывают создание карточки.
"""
import logging
import statistics

from database.db import db_connect
from database.repositories import DimsHistoryRepository

log = logging.getLogger(__name__)

# Медиана по последним образцам, а не по всей истории: ассортимент категории со
# временем меняется, и старые находки не должны перевешивать свежие.
HISTORY_WINDOW = 50

Dims = tuple[float, float, float, float]


def median_dims(samples: list[Dims]) -> Dims | None:
    """Покомпонентная медиана образцов (длина, ширина, высота, вес)."""
    if not samples:
        return None
    lens, wids, heis, wts = zip(*samples)
    return (statistics.median(lens), statistics.median(wids),
            statistics.median(heis), statistics.median(wts))


async def record_dims(subject_id: int, length: float, width: float,
                      height: float, weight: float, source: str = "") -> None:
    """Запоминает реально найденные габариты. Пишем только полный набор (все 4
    значения из одного источника), иначе медиана смешала бы длину одного товара
    с высотой другого."""
    try:
        async with db_connect() as db:
            await DimsHistoryRepository(db).add(subject_id, length, width, height, weight, source)
    except Exception as e:
        log.warning(f"wb_dims_history: не записалось (subject {subject_id}): {e}")


async def get_dims_median(subject_id: int) -> Dims | None:
    """Медиана последних HISTORY_WINDOW образцов категории. None — истории ещё
    нет или база недоступна; вызывающий код тогда берёт статичный дефолт.
    Раньше ошибка чтения отсюда роняла создание карточки целиком, хотя запись
    в историю уже была обёрнута в try/except."""
    try:
        async with db_connect() as db:
            samples = await DimsHistoryRepository(db).recent(subject_id, HISTORY_WINDOW)
    except Exception as e:
        log.warning(f"wb_dims_history: не прочиталось (subject {subject_id}): {e}")
        return None
    return median_dims(samples)
