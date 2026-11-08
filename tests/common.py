"""Общие помощники тестов."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.models import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quasar_assist.agent import agent_is_available
from custom_components.quasar_assist.const import (
    CONF_AGENT_ID,
    CONF_CONVERSATION_TTL,
    DOMAIN,
)


class Clock:
    """Подменяемые часы срока диалога."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class StubAgent(AbstractConversationAgent):
    """Ассистент, которого тест регистрирует вторым."""

    @property
    def supported_languages(self) -> list[str]:
        return ["ru"]

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        raise AssertionError("вызов должен идти через подмену async_converse")


class ConverseMock:
    """Подмена conversation.async_converse."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.speech = "Включила свет"
        self.error_type = False
        self.exc: BaseException | None = None
        self.index = 0

    async def __call__(
        self,
        hass: HomeAssistant,
        text: str,
        conversation_id: str | None,
        context: object,
        **kwargs: Any,
    ) -> ConversationResult:
        self.calls.append((text, conversation_id, context, kwargs))
        if self.exc is not None:
            raise self.exc
        self.index += 1
        return build_result(
            self.speech,
            f"conv-{self.index}",
            error_type=self.error_type,
        )


def build_result(
    speech: str,
    conversation_id: str | None,
    *,
    error_type: bool = False,
) -> ConversationResult:
    """Собрать результат Assist с речью plain.speech."""
    response = intent.IntentResponse(language="ru")
    if speech != "":
        response.async_set_speech(speech)
    if error_type:
        response.response_type = intent.IntentResponseType.ERROR
    return ConversationResult(response=response, conversation_id=conversation_id)


def responses_body(
    model: str,
    text: str | None,
    *,
    noise: bool = True,
    content: object | None = None,
) -> dict[str, Any]:
    """Тело Responses. По умолчанию с системным текстом, историей и tools."""
    if content is not None:
        user: dict[str, Any] = {"role": "user", "content": content}
    elif text is None:
        user = {}
    else:
        user = {"role": "user", "content": text}

    items: list[dict[str, Any]] = []
    if noise:
        items.append({"role": "system", "content": "Инструкции инструментов моста"})
        if text is not None or content is not None:
            items.extend(
                [
                    {"role": "user", "content": "старая реплика"},
                    {"role": "assistant", "content": "старый ответ"},
                ]
            )
    if user:
        items.append(user)
    if noise:
        items.extend(
            [
                {
                    "type": "function_call",
                    "call_id": "context-example",
                    "name": "current_time",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "context-example",
                    "output": '{"datetime":"2026-11-08T09:07:00+03:00"}',
                },
            ]
        )
    return {
        "model": model,
        "stream": False,
        "store": False,
        "max_output_tokens": 4096,
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "tools": [],
        "text": {"format": {"type": "json_object"}},
        "conversation_id": "from-client",
        "agent_id": "from-client",
        "input": items,
    }


def assert_completed(payload: dict[str, Any], text: str) -> None:
    """Проверить успешную форму ответа мосту."""
    assert payload["status"] == "completed"
    assert len(payload["output"]) == 1
    message = payload["output"][0]
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["status"] == "completed"
    assert message["content"] == [{"type": "output_text", "text": text}]
    assert text != ""
    dumped = str(payload)
    assert "function_call" not in dumped


async def async_prepare_conversation(hass: HomeAssistant) -> None:
    """Поднять ядро и диалог, без которых Assist в тесте не стартует."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    await hass.async_block_till_done()


async def async_register_agent(hass: HomeAssistant, title: str) -> str:
    """Зарегистрировать второго ассистента и вернуть его id."""
    await async_prepare_conversation(hass)
    holder = MockConfigEntry(domain="openai_conversation", title=title)
    holder.add_to_hass(hass)
    conversation.async_set_agent(hass, holder, StubAgent())
    return str(holder.entry_id)


def assert_same_entry(found: ConfigEntry | None, entry: MockConfigEntry) -> None:
    """Запись в реестре — тот же объект, что добавили.

    Сравнение внутри функции: иначе ``is`` с результатом
    ``async_get_entry`` сужает ``entry`` до ``ConfigEntry | None``.
    """
    assert found is entry


async def async_add_entry(
    hass: HomeAssistant,
    agent_id: str,
    *,
    ttl: int = 90,
    title: str = "Спальня",
    entry_id: str | None = None,
) -> MockConfigEntry:
    """Добавить запись quasar_assist в реестр и запустить её.

    Живой ассистент загружает запись. Пропавший оставляет её в реестре
    в состоянии SETUP_ERROR: повтор сам её не поднимет.
    """
    await async_prepare_conversation(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={CONF_AGENT_ID: agent_id, CONF_CONVERSATION_TTL: ttl},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    available = agent_is_available(hass, agent_id)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert_same_entry(hass.config_entries.async_get_entry(entry.entry_id), entry)
    if available:
        assert entry.state is ConfigEntryState.LOADED
    else:
        assert entry.state is ConfigEntryState.SETUP_ERROR
    return entry


def patch_converse(
    monkeypatch: pytest.MonkeyPatch,
    mock: ConverseMock,
) -> None:
    """Подменить вызов Assist в обработчике."""
    monkeypatch.setattr(
        "custom_components.quasar_assist.http.conversation.async_converse",
        mock,
    )


def patch_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """Подменить часы обработчика и состояния."""
    monkeypatch.setattr("custom_components.quasar_assist.http.utcnow", clock)
    monkeypatch.setattr("custom_components.quasar_assist.runtime.utcnow", clock)


ClientFactory = Callable[..., Any]
