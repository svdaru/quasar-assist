"""Заглушка fcntl: на Windows stdlib-модуля нет.

Home Assistant 2026.10 импортирует fcntl из runner до загрузки conftest.
Каталог подключает pytest через pythonpath, раньше автозагрузки плагина.
"""

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd: int, operation: int) -> None:
    """Не блокировать файл: тесты не поднимают второй процесс Home Assistant."""
    del fd, operation
