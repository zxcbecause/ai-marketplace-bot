import os

import pytest

from utils.runtime_env import IS_WINDOWS, acquire_singleton_lock, disk_root


@pytest.mark.skipif(IS_WINDOWS, reason="flock-ветка; на Windows используется msvcrt")
def test_second_instance_cannot_take_lock(tmp_path):
    lock = tmp_path / "data" / "bot.lock"
    first = acquire_singleton_lock(lock)
    assert first is not None
    assert lock.read_text() == str(os.getpid())

    second = acquire_singleton_lock(lock)
    assert second is None

    first.close()  # процесс «упал» — лок снимается
    third = acquire_singleton_lock(lock)
    assert third is not None
    third.close()


def test_disk_root():
    if IS_WINDOWS:
        assert disk_root(r"C:\AI-Bot-V2\data\bot.db") == "C:\\"
    else:
        assert disk_root("/app/data/bot.db") == "/"
        assert disk_root("./data/bot.db") == "/"
