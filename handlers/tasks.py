"""Общий реестр asyncio-задач для всех хендлеров. /cancel смотрит сюда."""
import asyncio
import logging

log = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}


async def _guarded(coro, message=None):
    """Раньше исключение, вылетевшее ДО первого внутреннего try/except самой
    задачи (например get_llm() падает на "database is locked"), уходило
    только в "Task exception was never retrieved" — пользователь не получал
    вообще никакого сообщения, команда выглядела зависшей навсегда.
    Оборачиваем любую задачу целиком: полный traceback в лог + попытка
    сообщить в чат, если message передан."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception(f"run_task упал без обработки: {e}")
        if message is not None:
            try:
                await message.answer(f"🔴 Внутренняя ошибка: {e}\nПопробуй команду ещё раз.")
            except Exception:
                pass


def run_task(user_id: int, coro, message=None) -> asyncio.Task:
    """Регистрирует корутину как отменяемую задачу пользователя.
    message (опционально) — куда сообщить, если задача упадёт необработанным
    исключением (см. _guarded)."""
    # Отменяем предыдущую задачу если ещё работает
    old = _tasks.get(user_id)
    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(_guarded(coro, message))
    _tasks[user_id] = task

    def _cleanup(t):
        # Колбэк отменённой старой задачи не должен выкидывать новую
        if _tasks.get(user_id) is t:
            _tasks.pop(user_id, None)

    task.add_done_callback(_cleanup)
    return task


def cancel_task(user_id: int) -> bool:
    """Отменяет активную задачу пользователя. Возвращает True если была задача."""
    task = _tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


def has_task(user_id: int) -> bool:
    task = _tasks.get(user_id)
    return bool(task and not task.done())
