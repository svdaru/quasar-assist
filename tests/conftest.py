"""Фикстуры тестов Quasar Assist."""

import asyncio
import sys
import warnings
from collections.abc import Generator

import pytest
import pytest_socket

pytest_plugins = "pytest_homeassistant_custom_component"

# Класс политики на Windows устарел до Python 3.16.
# Забираем его один раз: иначе DeprecationWarning всплывает при разборе аннотации.
if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _EventLoopPolicy = asyncio.AbstractEventLoopPolicy
        _WindowsSelectorEventLoopPolicy = asyncio.WindowsSelectorEventLoopPolicy

    @pytest.fixture(scope="session", autouse=True)
    def event_loop_policy() -> _EventLoopPolicy:
        """Тестовый цикл — SelectorEventLoop, не Proactor.

        Proactor оставляет задачу AcceptEx после остановки HTTP.
        Предупреждение об устаревшей политике глушим выше, чтобы pytest его не ронял.
        """
        return _WindowsSelectorEventLoopPolicy()


def pytest_configure(config: pytest.Config) -> None:
    """Обойти запрет сокетов и aiodns на Windows-цикле asyncio."""
    # Переопределение event_loop_policy устарело в pytest-asyncio.
    config.addinivalue_line(
        "filterwarnings",
        "ignore:Overriding the .event_loop_policy. fixture is deprecated"
        ":DeprecationWarning",
    )
    config.addinivalue_line(
        "filterwarnings",
        "ignore:asyncio.WindowsSelectorEventLoopPolicy is deprecated"
        ":DeprecationWarning",
    )

    pytest_socket.disable_socket = lambda *args, **kwargs: None
    pytest_socket.enable_socket()

    # Импорт после запрета сокетов: иначе aiodns успевает поднять pycares.
    import aiodns

    class _SilentChannel:
        """Канал без pycares: aiohttp на закрытии зовёт cancel."""

        def cancel(self) -> None:
            return

    def _dns_init(self: aiodns.DNSResolver, *_args: object, **_kwargs: object) -> None:
        """Не поднимать pycares: на Windows он оставляет поток и ломает цикл."""
        # В kwargs цикл имеет тип object: берём его, только если это цикл событий.
        loop = _kwargs.get("loop")
        self.loop = (
            loop
            if isinstance(loop, asyncio.AbstractEventLoop)
            else asyncio.get_event_loop()
        )
        self._channel = _SilentChannel()  # type: ignore[assignment]
        self._read_fds = set()
        self._write_fds = set()
        self._closed = True
        self._timer = None
        self._event_thread = False

    def _dns_cancel(self: aiodns.DNSResolver) -> None:
        """Без канала pycares закрытие резолвера не должно падать."""
        channel = getattr(self, "_channel", None)
        if channel is None:
            return
        channel.cancel()

    aiodns.DNSResolver.__init__ = _dns_init  # type: ignore[method-assign]
    aiodns.DNSResolver.cancel = _dns_cancel  # type: ignore[method-assign]

    # Импорт после подмены DNSResolver: плагин связывает резолвер при загрузке.
    from pytest_homeassistant_custom_component import plugins

    class _Resolver:
        """Заглушка DNS: aiodns на ProactorEventLoop не поднимается."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        async def resolve(
            self, host: str, port: int = 0, family: int = 0
        ) -> list[object]:
            return []

        async def close(self) -> None:
            return

    plugins.AsyncResolver = _Resolver


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: object,
) -> Generator[None]:
    """Подключить custom_components этого репозитория."""
    yield
    del enable_custom_integrations
