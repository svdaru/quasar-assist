"""Общие помощники тестов."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from homeassistant.components import conversation
from homeassistant.components.assist_pipeline.pipeline import PipelineInput
from homeassistant.components.assist_pipeline.runtime import KEY_ASSIST_PIPELINE
from homeassistant.components.conversation.models import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.chat_session import async_get_chat_session
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quasar_assist.const import (
    CONF_CONVERSATION_TTL,
    CONF_PIPELINE_ID,
    DOMAIN,
)
from custom_components.quasar_assist.pipeline import pipeline_is_available


class Clock:
    """Подменяемые часы срока диалога."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class SpeechEngine(AbstractConversationAgent):
    """Движок конвейера: речь задаёт тест, сеть модели не нужна."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.speech = "Включила свет"
        self.error_type = False
        self.exc: BaseException | None = None

    @property
    def supported_languages(self) -> list[str]:
        return ["en", "ru"]

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        self.calls.append(
            (
                user_input.text,
                user_input.conversation_id,
                user_input.extra_system_prompt,
            )
        )
        if self.exc is not None:
            raise self.exc
        response = intent.IntentResponse(language=user_input.language)
        if self.speech != "":
            response.async_set_speech(self.speech)
        if self.error_type:
            response.response_type = intent.IntentResponseType.ERROR
            response.error_code = intent.IntentResponseErrorCode.NO_INTENT_MATCH
        return ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id,
        )


class PipelineCapture:
    """Запуски PipelineInput, которые увидел тест."""

    def __init__(self) -> None:
        self.inputs: list[PipelineInput] = []
        self.events: list[list[Any]] = []


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
        "pipeline_id": "from-client",
        "language": "de",
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


def intent_speech(events: list[Any]) -> str:
    """Речь единственного события intent-end."""
    speeches = [
        event.data["intent_output"]["response"]["speech"]["plain"]["speech"]
        for event in events
        if getattr(event, "type", None) == "intent-end" and isinstance(event.data, dict)
    ]
    assert len(speeches) == 1
    assert isinstance(speeches[0], str)
    return speeches[0]


async def async_prepare_assist(hass: HomeAssistant) -> None:
    """Поднять ядро и конвейер Assist без живой колонки."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()


async def async_create_pipeline(
    hass: HomeAssistant,
    name: str,
    engine: SpeechEngine | None = None,
    *,
    language: str = "en",
) -> tuple[str, SpeechEngine]:
    """Создать конвейер с локальным движком речи и вернуть его id."""
    await async_prepare_assist(hass)
    if engine is None:
        engine = SpeechEngine()
    holder = MockConfigEntry(domain="pipeline_engine_test", title=name)
    holder.add_to_hass(hass)
    conversation.async_set_agent(hass, holder, engine)
    created = await hass.data[KEY_ASSIST_PIPELINE].pipeline_store.async_create_item(
        {
            "conversation_engine": holder.entry_id,
            "conversation_language": language,
            "language": language,
            "name": name,
            "stt_engine": None,
            "stt_language": None,
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
            "prefer_local_intents": False,
        }
    )
    return created.id, engine


def assert_same_entry(found: ConfigEntry | None, entry: MockConfigEntry) -> None:
    """Запись в реестре — тот же объект, что добавили.

    Сравнение внутри функции: иначе ``is`` с результатом
    ``async_get_entry`` сужает ``entry`` до ``ConfigEntry | None``.
    """
    assert found is entry


async def async_add_entry(
    hass: HomeAssistant,
    pipeline_id: str,
    *,
    ttl: int = 90,
    title: str = "Спальня",
    entry_id: str | None = None,
) -> MockConfigEntry:
    """Добавить запись quasar_assist и запустить её.

    Живой конвейер загружает запись. Пропавший оставляет её в реестре
    в состоянии SETUP_ERROR: повтор сам её не поднимет.
    """
    await async_prepare_assist(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: ttl},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    available = pipeline_is_available(hass, pipeline_id)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert_same_entry(hass.config_entries.async_get_entry(entry.entry_id), entry)
    if available:
        assert entry.state is ConfigEntryState.LOADED
    else:
        assert entry.state is ConfigEntryState.SETUP_ERROR
    return entry


def patch_execute(monkeypatch: pytest.MonkeyPatch, capture: PipelineCapture) -> None:
    """Запомнить каждый PipelineInput и события его запуска."""
    original = PipelineInput.execute

    async def wrapped(self: PipelineInput, validate: bool = False) -> None:
        capture.inputs.append(self)
        events: list[Any] = []
        original_callback = self.run.event_callback

        def spy(event: Any) -> None:
            events.append(event)
            original_callback(event)

        self.run.event_callback = spy
        capture.events.append(events)
        await original(self, validate=validate)

    monkeypatch.setattr(PipelineInput, "execute", wrapped)


def patch_session_ids(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Запомнить идентификаторы диалога, которые запись передала в сессию."""
    recorded: list[str | None] = []
    original = async_get_chat_session

    def wrapped(hass: HomeAssistant, conversation_id: str | None = None) -> Any:
        recorded.append(conversation_id)
        return original(hass, conversation_id)

    monkeypatch.setattr(
        "custom_components.quasar_assist.pipeline.async_get_chat_session",
        wrapped,
    )
    return recorded


def patch_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """Подменить часы обработчика и состояния."""
    monkeypatch.setattr("custom_components.quasar_assist.http.utcnow", clock)
    monkeypatch.setattr("custom_components.quasar_assist.runtime.utcnow", clock)


async def async_delete_pipeline(hass: HomeAssistant, pipeline_id: str) -> None:
    """Удалить конвейер из хранилища Assist."""
    await hass.data[KEY_ASSIST_PIPELINE].pipeline_store.async_delete_item(pipeline_id)


ClientFactory = Callable[..., Any]
