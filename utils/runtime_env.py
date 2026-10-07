"""Кроссплатформенные мелочи: бот работает и на Windows-ПК, и в Docker на Linux."""
import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def in_docker() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("RUNNING_IN_DOCKER") == "1"


def acquire_singleton_lock(lock_path: str | os.PathLike):
    """Эксклюзивная блокировка файла: второй экземпляр бота получит False.

    Блокировка привязана к открытому файлу, поэтому ОС снимает её сама, даже если
    процесс убили, и протухший лок не мешает следующему запуску.
    Возвращает открытый файл (держать до конца работы) или None, если лок занят.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def disk_root(path: str | os.PathLike) -> str:
    """Корень диска, на котором лежит путь: 'C:\\' на Windows, '/' на Linux."""
    anchor = Path(os.path.abspath(path)).anchor
    return anchor or os.sep

