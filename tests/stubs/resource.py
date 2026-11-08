"""Заглушка resource: на Windows stdlib-модуля нет.

Home Assistant читает лимит дескрипторов при импорте runner.
Тесты этот лимит не меняют.
"""

RLIMIT_NOFILE = 7


def getrlimit(resource: int) -> tuple[int, int]:
    """Вернуть мягкий и жёсткий лимит, не трогая процесс."""
    del resource
    return (2048, 2048)


def setrlimit(resource: int, limits: tuple[int, int]) -> None:
    """Игнорировать смену лимита: на Windows resource.setrlimit нет."""
    del resource, limits
