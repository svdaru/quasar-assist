"""Ветка маршрута classify и отладочная строка хода."""

import asyncio
import json
import logging
from typing import Any

import pytest
from aiohttp.client_exceptions import ServerDisconnectedError
from hassil.intents import Intent, IntentData
from hassil.models import UnmatchedTextEntity
from hassil.recognize import RecognizeResult
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from homeassistant.components.automation import DATA_COMPONENT, AutomationEntity
from homeassistant.components.conversation.agent_manager import get_agent_manager
from homeassistant.components.conversation.const import HOME_ASSISTANT_AGENT
from homeassistant.components.conversation.default_agent import DefaultAgent
from homeassistant.components.conversation.models import ConversationInput
from homeassistant.components.conversation.trigger import async_attach_trigger
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.trigger import TriggerInfo, async_initialize_triggers
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.quasar_assist.const import (
    MODELS_URL,
    PHRASE_EMPTY,
    PHRASE_NOT_FOUND,
    RESPONSES_URL,
)
from custom_components.quasar_assist.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.quasar_assist.http import (
    ResponsesView,
    _TurnLog,
    async_load_json_body,
)
from tests.common import (
    PipelineCapture,
    assert_completed,
    async_add_entry,
    async_create_pipeline,
    async_delete_pipeline,
    intent_speech,
    patch_execute,
)

PROBE_EVENT = "route_probe_fired"
HISTORY = "старая реплика"
OLD_ANSWER = "старый ответ"
SYSTEM_SECRET = "секрет-системы"
_HIDDEN_KEYS = ("conversation_id", "pipeline_id")


def classify_body(
    model: str,
    text: str | None,
    *,
    system: object = "classify",
    later_system: str | None = None,
    language: str | None = None,
    history: bool = True,
) -> dict[str, Any]:
    """Тело с первым system. История и язык тела — шум, если их попросили."""
    items: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if later_system is not None:
        items.append({"role": "system", "content": later_system})
    if history:
        items.extend(
            [
                {"role": "user", "content": HISTORY},
                {"role": "assistant", "content": OLD_ANSWER},
            ]
        )
    if text is not None:
        items.append({"role": "user", "content": text})
    body: dict[str, Any] = {
        "model": model,
        "conversation_id": "from-client",
        "pipeline_id": "from-client",
        "input": items,
    }
    if language is not None:
        body["language"] = language
    return body


def responses_without_route(model: str, text: str) -> dict[str, Any]:
    """Обычный запрос конвейера с историей и системным текстом."""
    return classify_body(
        model,
        text,
        system="Инструкции инструментов моста",
        later_system=SYSTEM_SECRET,
    )


def automation_item(
    alias: str,
    command: str | list[str],
    *,
    list_key: str = "triggers",
    type_key: str = "platform",
    enabled: bool | str | None = None,
    initial_state: bool | None = None,
    action_key: str = "actions",
) -> dict[str, Any]:
    """Автоматизация с одним триггером conversation и событием вместо действия."""
    trigger: dict[str, Any] = {type_key: "conversation", "command": command}
    if enabled is not None:
        trigger["enabled"] = enabled
    item: dict[str, Any] = {
        "alias": alias,
        list_key: [trigger],
        action_key: [{"event": PROBE_EVENT, "event_data": {"alias": alias}}],
    }
    if initial_state is not None:
        item["initial_state"] = initial_state
    return item


def spoken(text: str, language: str) -> ConversationInput:
    """Вход агента: язык задан явно, колонка не устройство и не спутник."""
    return ConversationInput(
        text=text,
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language=language,
        agent_id=HOME_ASSISTANT_AGENT,
    )


async def async_setup_automations(
    hass: HomeAssistant, items: list[dict[str, Any]]
) -> None:
    """Поднять встроенного агента и автоматизации. Неудачная схема роняет тест."""
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "automation", {"automation": items})
    await hass.async_block_till_done()


async def async_builtin_agent(hass: HomeAssistant) -> DefaultAgent:
    """Поднять встроенного агента и вернуть его."""
    assert await async_setup_component(hass, "conversation", {})
    await hass.async_block_till_done()
    agent = get_agent_manager(hass).default_agent
    assert agent is not None
    return agent


def put_light(hass: HomeAssistant, entity_id: str, name: str, *, exposed: bool) -> None:
    """Поставить свет и явно открыть или закрыть его голосовому помощнику."""
    hass.states.async_set(entity_id, "off", {"friendly_name": name})
    async_expose_entity(hass, "conversation", entity_id, exposed)


def listen(hass: HomeAssistant) -> list[Any]:
    """События действия автоматизации после этой подписки."""
    found: list[Any] = []
    hass.bus.async_listen(PROBE_EVENT, lambda event: found.append(event))
    return found


def service_names(hass: HomeAssistant) -> list[tuple[str | None, str | None]]:
    """Имена служб, которые вызвали после этой подписки."""
    found: list[tuple[str | None, str | None]] = []

    def _remember(event: Any) -> None:
        data = event.data
        domain = data.get("domain")
        service = data.get("service")
        found.append(
            (
                domain if isinstance(domain, str) else None,
                service if isinstance(service, str) else None,
            )
        )

    hass.bus.async_listen("call_service", _remember)
    return found


def forbid_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запуск конвейера из адреса — ошибка теста."""

    async def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("конвейер запущен")

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.async_run_pipeline",
        _forbidden,
    )


def forbid_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Исполнение триггера, намерения и автоматизации — ошибка теста."""

    async def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("исполнение")

    monkeypatch.setattr(DefaultAgent, "async_handle_sentence_triggers", _forbidden)
    monkeypatch.setattr(DefaultAgent, "async_handle_intents", _forbidden)
    monkeypatch.setattr(AutomationEntity, "async_trigger", _forbidden)


def watch_route(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Запомнить вызов маршрута и вернуть llm, если его всё же вызвали."""
    seen: list[str] = []

    async def _called(*_args: object, **_kwargs: object) -> str:
        seen.append("route")
        return "llm"

    monkeypatch.setattr("custom_components.quasar_assist.http.match_route", _called)
    return seen


def own_messages(
    caplog: pytest.LogCaptureFixture, *, level: int | None = None
) -> list[str]:
    """Сообщения журнала интеграции, по желанию одного уровня."""
    messages: list[str] = []
    for record in caplog.records:
        if not record.name.startswith("custom_components.quasar_assist"):
            continue
        if level is not None and record.levelno != level:
            continue
        messages.append(record.getMessage())
    return messages


def assert_no_route_literals(messages: list[str]) -> None:
    """В строках модуля нет литералов маршрута."""
    for message in messages:
        assert "llm" not in message
        assert "yandex" not in message


async def read_route(response: Any) -> str:
    """Разобрать единственное поле route. home в тексте нет."""
    assert response.status == 200
    payload = await response.json()
    text = payload["output"][0]["content"][0]["text"]
    assert isinstance(text, str)
    assert_completed(payload, text)
    parsed = json.loads(text)
    assert set(parsed) == {"route"}
    assert parsed["route"] in {"llm", "yandex"}
    assert "home" not in text
    route = parsed["route"]
    assert isinstance(route, str)
    return route


async def assert_empty_500(response: Any) -> None:
    """HTTP 500 с действительно пустым телом."""
    assert response.status == 500
    assert await response.read() == b""


class _Guard(dict[str, Any]):
    """Тело, которое не отдаёт идентификатор диалога и конвейер."""

    def __getitem__(self, key: str) -> Any:
        if key in _HIDDEN_KEYS:
            raise AssertionError(key)
        return dict.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in _HIDDEN_KEYS:
            raise AssertionError(key)
        return dict.get(self, key, default)


async def test_automation_trigger_is_llm(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-01: триггер автоматизации даёт llm, действие и конвейер молчат."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    phrase = "включи лампу"
    await async_setup_automations(hass, [automation_item("Лампа", phrase)])
    calls: list[str] = []

    def _refresh(*_args: object, **_kwargs: object) -> bool:
        calls.append("refresh")
        return True

    def _close(*_args: object, **_kwargs: object) -> None:
        calls.append("close")

    def _remember(*_args: object, **_kwargs: object) -> None:
        calls.append("remember")

    def _hold(*_args: object, **_kwargs: object) -> None:
        calls.append("hold")

    def _register(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("register_trigger")

    original_trigger = DefaultAgent.async_recognize_sentence_trigger
    seen: list[str] = []

    async def _trigger(self: DefaultAgent, user_input: ConversationInput) -> Any:
        seen.append("trigger")
        assert user_input.text == phrase
        assert user_input.language == hass.config.language
        assert user_input.device_id is None
        assert user_input.satellite_id is None
        return await original_trigger(self, user_input)

    async def _intent(
        self: DefaultAgent,
        user_input: ConversationInput,
        strict_intents_only: bool = False,
    ) -> Any:
        del self, user_input, strict_intents_only
        seen.append("intent")
        raise AssertionError("async_recognize_intent")

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.refresh_pipeline", _refresh
    )
    monkeypatch.setattr("custom_components.quasar_assist.http.close_dialogue", _close)
    monkeypatch.setattr(
        "custom_components.quasar_assist.http.remember_success", _remember
    )
    monkeypatch.setattr("custom_components.quasar_assist.http.hold_chat_session", _hold)
    monkeypatch.setattr(
        "homeassistant.components.conversation.agent_manager.AgentManager.register_trigger",
        _register,
    )
    monkeypatch.setattr(DefaultAgent, "async_recognize_sentence_trigger", _trigger)
    monkeypatch.setattr(DefaultAgent, "async_recognize_intent", _intent)
    forbid_execution(monkeypatch)
    fired = listen(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase),
    )
    assert await read_route(response) == "llm"
    assert seen == ["trigger"]
    assert capture.inputs == []
    assert fired == []
    assert calls == []
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_script_trigger_is_llm(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-02: триггер сценария даёт llm, сценарий не выполняется."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    phrase = "фраза сценария"
    await async_builtin_agent(hass)
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "probe_script": {
                    "alias": "Проба",
                    "sequence": [{"event": PROBE_EVENT}],
                }
            }
        },
    )
    await hass.async_block_till_done()
    ran: list[str] = []

    async def _action(
        run_variables: dict[str, Any], context: Context | None = None
    ) -> None:
        del run_variables, context
        ran.append("script")
        await hass.services.async_call("script", "probe_script", blocking=True)

    def _silent(*_args: object, **_kwargs: object) -> None:
        return

    remove = await async_initialize_triggers(
        hass,
        [{"platform": "conversation", "command": [phrase]}],
        _action,
        "script",
        "probe_script",
        _silent,
    )
    assert remove is not None
    assert phrase in get_agent_manager(hass).trigger_sentences
    assert hass.data.get(DATA_COMPONENT) is None
    fired = listen(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(response) == "llm"
    assert ran == []
    assert fired == []
    state = hass.states.get("script.probe_script")
    assert state is not None
    assert state.attributes.get("last_triggered") is None
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_strict_intent_is_llm(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03: строгое намерение открытой сущности даёт llm без исполнения."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_builtin_agent(hass)
    put_light(hass, "light.kitchen", "kitchen", exposed=True)
    phrase = "turn on kitchen"
    services = service_names(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(response) == "llm"
    kitchen = hass.states.get("light.kitchen")
    assert kitchen is not None
    assert kitchen.state == "off"
    assert ("light", "turn_on") not in services


async def test_unexposed_name_is_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-04: имя не среди открытых сущностей — yandex, намерение не исполняется."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_builtin_agent(hass)
    put_light(hass, "light.bedlamp", "bedlamp", exposed=False)
    services = service_names(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "turn on bedlamp", history=False),
    )
    assert await read_route(response) == "yandex"
    bedlamp = hass.states.get("light.bedlamp")
    assert bedlamp is not None
    assert bedlamp.state == "off"
    assert ("light", "turn_on") not in services
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_unmatched_entities_are_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Поле unmatched_entities: пустое даёт llm, непустое — yandex."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_builtin_agent(hass)
    unmatched = RecognizeResult(
        intent=Intent("HassTurnOn"),
        intent_data=IntentData([]),
        unmatched_entities={"name": UnmatchedTextEntity("name", "спальня")},
    )
    matched = RecognizeResult(
        intent=Intent("HassTurnOn"),
        intent_data=IntentData([]),
    )
    assert not matched.unmatched_entities
    queued = [unmatched, matched]
    flags: list[bool] = []

    async def _intent(
        self: DefaultAgent,
        user_input: ConversationInput,
        strict_intents_only: bool = False,
    ) -> RecognizeResult:
        del self
        assert user_input.device_id is None
        assert user_input.satellite_id is None
        flags.append(strict_intents_only)
        return queued.pop(0)

    monkeypatch.setattr(DefaultAgent, "async_recognize_intent", _intent)
    client = await hass_client()
    hidden = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "turn on спальня", history=False),
    )
    clean = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "turn on kitchen", history=False),
    )
    assert await read_route(hidden) == "yandex"
    assert await read_route(clean) == "llm"
    assert flags == [True, True]


async def test_no_trigger_and_no_intent_is_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-05: ни триггер, ни строгое намерение — yandex."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_builtin_agent(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "фразу никто не знает", history=False),
    )
    assert await read_route(response) == "yandex"
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_disabled_automation_is_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-06: выключенная автоматизация без сценария и намерения — yandex."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    phrase = "команда выключенной автоматизации"
    await async_setup_automations(
        hass,
        [automation_item("Выключена", phrase, initial_state=False)],
    )
    assert phrase not in get_agent_manager(hass).trigger_sentences
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(response) == "yandex"


async def test_without_classify_runs_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-07: без ветки фраза идёт в конвейер, наш маршрут не вызывается."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = "Включила свет"
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    seen = watch_route(monkeypatch)
    client = await hass_client()
    body = classify_body(
        entry.entry_id,
        "включи свет",
        system="Инструкции инструментов моста",
        history=False,
    )
    first = await client.post(RESPONSES_URL, json=body)
    assert first.status == 200
    payload = await first.json()
    speech = intent_speech(capture.events[0])
    assert_completed(payload, speech)
    assert speech == "Включила свет"
    assert json.dumps(payload).find('{"route"') == -1
    # Нет встроенного агента: запрос без ветки не становится пустым 500.
    get_agent_manager(hass).default_agent = None
    second = await client.post(RESPONSES_URL, json=body)
    assert second.status == 200
    second_payload = await second.json()
    second_text = second_payload["output"][0]["content"][0]["text"]
    assert isinstance(second_text, str)
    assert "route" not in second_text
    assert len(capture.inputs) == 2
    assert seen == []
    assert capture.inputs[0].run.start_stage.name == "INTENT"
    assert capture.inputs[0].run.end_stage.name == "INTENT"


async def test_missing_pipeline_still_classifies(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-08: пропавший конвейер не мешает классификации и не закрывает диалог."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    entry.runtime_data.pipeline_unavailable = False
    await async_setup_automations(hass, [automation_item("Лампа", "включи лампу")])
    await async_delete_pipeline(hass, pipeline_id)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    assert await read_route(response) == "llm"
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment
    assert entry.runtime_data.pipeline_unavailable is False

    bare = await async_add_entry(hass, "missing-pipeline", entry_id="bare-entry")
    assert getattr(bare, "runtime_data", None) is None
    bare_response = await client.post(
        RESPONSES_URL,
        json=classify_body(bare.entry_id, "включи лампу", history=False),
    )
    assert await read_route(bare_response) == "llm"
    assert getattr(bare, "runtime_data", None) is None


async def test_home_assistant_language(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-09: совпадение только при языке Home Assistant, без устройства."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня", language="en")
    stored = async_get_pipeline(hass, pipeline_id)
    assert stored.conversation_language == "en"
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    hass.config.language = "en"
    agent = await async_builtin_agent(hass)
    trigger_phrase = "pegel zwanzig"
    intent_phrase = "schalte kuche an"
    await async_setup_automations(
        hass,
        [automation_item("Уровень", "pegel {1..30:n}")],
    )
    put_light(hass, "light.kuche", "kuche", exposed=True)
    hass.config.language = "en"
    agent._trigger_intents = None
    assert (
        await agent.async_recognize_sentence_trigger(spoken(trigger_phrase, "en"))
        is None
    )
    hass.config.language = "ru"
    agent._trigger_intents = None
    assert (
        await agent.async_recognize_sentence_trigger(spoken(trigger_phrase, "ru"))
        is None
    )
    hass.config.language = "de"
    agent._trigger_intents = None
    assert (
        await agent.async_recognize_sentence_trigger(spoken(trigger_phrase, "ru"))
        is not None
    )
    assert (
        await agent.async_recognize_intent(
            spoken(intent_phrase, "en"), strict_intents_only=True
        )
        is None
    )
    assert (
        await agent.async_recognize_intent(
            spoken(intent_phrase, "ru"), strict_intents_only=True
        )
        is None
    )
    german = await agent.async_recognize_intent(
        spoken(intent_phrase, "de"), strict_intents_only=True
    )
    assert german is not None
    assert not german.unmatched_entities
    assert stored.conversation_language == "en"
    assert hass.config.language == "de"
    calls: list[tuple[str, dict[str, Any], ConversationInput]] = []
    original_trigger = DefaultAgent.async_recognize_sentence_trigger
    original_intent = DefaultAgent.async_recognize_intent

    async def _trigger(
        self: DefaultAgent, user_input: ConversationInput, **kwargs: Any
    ) -> Any:
        calls.append(("trigger", kwargs, user_input))
        return await original_trigger(self, user_input, **kwargs)

    async def _intent(
        self: DefaultAgent,
        user_input: ConversationInput,
        strict_intents_only: bool = False,
        **kwargs: Any,
    ) -> Any:
        calls.append(
            (
                "intent",
                {"strict_intents_only": strict_intents_only, **kwargs},
                user_input,
            )
        )
        return await original_intent(
            self,
            user_input,
            strict_intents_only=strict_intents_only,
            **kwargs,
        )

    monkeypatch.setattr(DefaultAgent, "async_recognize_sentence_trigger", _trigger)
    monkeypatch.setattr(DefaultAgent, "async_recognize_intent", _intent)
    client = await hass_client()
    trigger_body = classify_body(
        entry.entry_id,
        trigger_phrase,
        history=False,
        language="ru",
    )
    intent_body = classify_body(
        entry.entry_id,
        intent_phrase,
        history=False,
        language="ru",
    )
    assert trigger_body["language"] == "ru"
    triggered = await client.post(RESPONSES_URL, json=trigger_body)
    intended = await client.post(RESPONSES_URL, json=intent_body)
    assert await read_route(triggered) == "llm"
    assert await read_route(intended) == "llm"
    assert [item[0] for item in calls] == ["trigger", "trigger", "intent"]
    for _kind, kwargs, user_input in calls:
        assert "device_id" not in kwargs
        assert "satellite_id" not in kwargs
        assert user_input.device_id is None
        assert user_input.satellite_id is None
        assert user_input.language == "de"
    assert calls[2][1]["strict_intents_only"] is True
    kuche = hass.states.get("light.kuche")
    assert kuche is not None
    assert kuche.state == "off"


async def test_classify_debug_line(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-10: одна отладочная строка с репликой и маршрутом, без истории."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = "речь-конвейера-которой-нет"
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(hass, [automation_item("Лампа", "включи лампу")])
    client = await hass_client()
    phrase = "включи лампу"
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(
            entry.entry_id,
            phrase,
            later_system=SYSTEM_SECRET,
            language="ru",
        ),
    )
    assert await read_route(response) == "llm"
    debug = own_messages(caplog, level=logging.DEBUG)
    own = "\n".join(own_messages(caplog))
    assert len(debug) == 1
    assert phrase in debug[0]
    assert "маршрут=llm" in debug[0]
    assert "речь-конвейера-которой-нет" not in own
    assert HISTORY not in own
    assert OLD_ANSWER not in own
    assert SYSTEM_SECRET not in own
    assert "classify" not in own
    assert hass_access_token not in own
    assert "Bearer" not in own
    assert "authorization" not in own.lower()


async def test_pipeline_debug_line(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Одна отладочная строка конвейера содержит реплику и речь, токена нет."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    phrase = "реплика-хода"
    speech = "речь-ответа-конвейера"
    engine.speech = speech
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    response = await client.post(
        RESPONSES_URL,
        json=responses_without_route(entry.entry_id, phrase),
    )
    assert response.status == 200
    assert_completed(await response.json(), speech)
    debug = own_messages(caplog, level=logging.DEBUG)
    own = "\n".join(own_messages(caplog))
    assert len(debug) == 1
    assert phrase in debug[0]
    assert speech in debug[0]
    assert hass_access_token not in own
    assert "Bearer" not in own
    assert "authorization" not in own.lower()


async def test_registered_trigger_copy_is_llm(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-12: уже зарегистрированный триггер даёт llm и ничего не исполняет."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_builtin_agent(hass)
    phrase = "фраза копии"
    ran: list[object] = []

    async def _action(
        run_variables: dict[str, Any], context: Context | None = None
    ) -> None:
        del context
        ran.append(run_variables)

    info: TriggerInfo = {
        "domain": "conversation",
        "name": "copy",
        "variables": None,
        "trigger_data": {"id": "copy", "idx": "0", "alias": None},
    }
    remove = await async_attach_trigger(
        hass,
        {"platform": "conversation", "command": [phrase]},
        _action,
        info,
    )
    assert remove is not None
    assert phrase in get_agent_manager(hass).trigger_sentences
    saved = [
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.startswith(("automation.", "script."))
    ]
    assert saved == []
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(response) == "llm"
    assert ran == []


async def test_optional_fragment_and_slot_match(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-13: необязательный фрагмент и подстановка дают llm без запуска."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [
            automation_item("Вежливо", "включи [пожалуйста] свет"),
            automation_item("Комната", "включи {комната} лампу"),
        ],
    )
    fired = listen(hass)
    client = await hass_client()
    phrases = ("включи свет", "включи пожалуйста свет", "включи кухню лампу")
    for phrase in phrases:
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, phrase, history=False),
        )
        assert await read_route(response) == "llm"
    assert fired == []


async def test_second_command_and_triggers_key(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-14: вторая команда и ключ triggers дают llm через регистрацию."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [
            automation_item("Список", ["первая команда", "вторая команда"]),
            automation_item(
                "Новый",
                "команда нового ключа",
                list_key="triggers",
                type_key="trigger",
            ),
        ],
    )
    sentences = get_agent_manager(hass).trigger_sentences
    assert "вторая команда" in sentences
    assert "команда нового ключа" in sentences
    client = await hass_client()
    for phrase in ("вторая команда", "команда нового ключа"):
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, phrase, history=False),
        )
        assert await read_route(response) == "llm"


async def test_disabled_trigger_is_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-15: автоматизация включена, её триггер выключен — yandex."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    phrase = "команда выключенного триггера"
    await async_setup_automations(
        hass,
        [automation_item("Включена", phrase, enabled=False)],
    )
    assert phrase not in get_agent_manager(hass).trigger_sentences
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(response) == "yandex"


async def test_pipeline_agent_is_not_called(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-16: решает встроенный агент, агент конвейера не вызывается."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    stored = async_get_pipeline(hass, pipeline_id)
    assert stored.conversation_engine != HOME_ASSISTANT_AGENT
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("агент конвейера")

    engine.async_recognize_sentence_trigger = _refuse  # type: ignore[attr-defined]
    engine.async_recognize_intent = _refuse  # type: ignore[attr-defined]
    await async_setup_automations(hass, [automation_item("Лампа", "включи лампу")])
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    assert await read_route(response) == "llm"
    assert engine.calls == []


async def test_route_text_has_no_home(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-17: ни триггер, ни намерение, ни промах не пишут home."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    forbid_execution(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(hass, [automation_item("Лампа", "включи лампу")])
    put_light(hass, "light.kitchen", "kitchen", exposed=True)
    client = await hass_client()
    phrases = ("включи лампу", "turn on kitchen", "фразу никто не знает")
    routes: list[str] = []
    for phrase in phrases:
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, phrase, history=False),
        )
        routes.append(await read_route(response))
    assert routes == ["llm", "llm", "yandex"]


async def test_higher_levels_and_diagnostics_hide_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-18: информация, предупреждение, ошибка и диагностика без реплики."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    phrase = "реплика-диагностики"
    pipeline_phrase = "фраза-конвейера"
    speech = "речь-диагностики"
    engine.speech = speech
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(hass, [automation_item("Лампа", phrase)])
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    classified = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(classified) == "llm"
    pipelined = await client.post(
        RESPONSES_URL,
        json=responses_without_route(entry.entry_id, pipeline_phrase),
    )
    assert_completed(await pipelined.json(), speech)
    higher = "\n".join(
        message
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
        and record.levelno > logging.DEBUG
        for message in (record.getMessage(),)
    )
    assert phrase not in higher
    assert pipeline_phrase not in higher
    assert speech not in higher
    dumped = json.dumps(
        await async_get_config_entry_diagnostics(hass, entry),
        ensure_ascii=False,
    )
    assert phrase not in dumped
    assert pipeline_phrase not in dumped
    assert speech not in dumped


async def test_foreign_form_skips_agent_and_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-N01: чужая форма — 400, без конвейера, методов и тела в журнале."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    seen = watch_route(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    marker = "секрет-чужого-тела"
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    rejected = [
        await client.post(
            RESPONSES_URL,
            data=marker.encode(),
            headers={"Content-Type": "application/json"},
        ),
        await client.post(
            RESPONSES_URL,
            json={"model": entry.entry_id, "note": marker},
        ),
        await client.post(
            RESPONSES_URL,
            json={
                "model": entry.entry_id,
                "input": [{"role": "user", "content": {"text": marker}}],
            },
        ),
    ]
    for response in rejected:
        assert response.status == 400
    assert seen == []
    own = "\n".join(own_messages(caplog))
    assert marker not in own
    assert len(own_messages(caplog, level=logging.DEBUG)) == len(rejected)


async def test_unknown_entry_answers_before_empty_or_classify(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N02: неизвестная запись — короткая фраза, не маршрут и не 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    seen = watch_route(monkeypatch)
    await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    bodies = [
        classify_body("нет-такой-записи", "включи лампу"),
        classify_body("нет-такой-записи", ""),
        classify_body("нет-такой-записи", None),
        classify_body(
            "нет-такой-записи",
            "включи лампу",
            system="Инструкции инструментов моста",
        ),
        classify_body(
            "нет-такой-записи",
            "",
            system="Инструкции инструментов моста",
        ),
    ]
    for body in bodies:
        response = await client.post(RESPONSES_URL, json=body)
        assert response.status == 200
        payload = await response.json()
        text = payload["output"][0]["content"][0]["text"]
        assert_completed(payload, text)
        assert text == PHRASE_NOT_FOUND
        assert "route" not in text
    assert seen == []


async def test_classify_branch_not_enabled(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N06: пробел, нестроковый system и позднее classify не включают ветку."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = "Включила свет"
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    seen = watch_route(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    bodies = [
        classify_body(entry.entry_id, "включи свет", system=" classify"),
        classify_body(
            entry.entry_id,
            "включи свет",
            system={"text": "classify"},
        ),
        classify_body(
            entry.entry_id,
            "включи свет",
            system="другие инструкции",
            later_system="classify",
        ),
    ]
    for body in bodies:
        response = await client.post(RESPONSES_URL, json=body)
        assert response.status == 200
        payload = await response.json()
        assert_completed(payload, "Включила свет")
        assert "route" not in payload["output"][0]["content"][0]["text"]
    assert len(capture.inputs) == len(bodies)
    assert seen == []


async def test_empty_phrase_without_classify_keeps_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N03: пустая фраза без ветки — короткая фраза, диалог на месте."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    seen = watch_route(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(
            entry.entry_id,
            "",
            system="Инструкции инструментов моста",
            history=False,
        ),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_EMPTY)
    assert seen == []
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_empty_phrase_with_classify_is_empty_500(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-N04: пустая фраза найденной записи на ветке — пустой 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    seen = watch_route(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    for text in ("", None):
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, text, history=False),
        )
        await assert_empty_500(response)
        assert entry.runtime_data.conversation_id == "live-dialogue"
        assert entry.runtime_data.last_success == moment
    assert seen == []
    assert_no_route_literals(own_messages(caplog))


async def test_missing_builtin_agent_is_empty_500(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-N05: нет встроенного агента — пустой 500, методы не вызываются."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    calls: list[str] = []

    async def _called(*_args: object, **_kwargs: object) -> None:
        calls.append("method")

    monkeypatch.setattr(DefaultAgent, "async_recognize_sentence_trigger", _called)
    monkeypatch.setattr(DefaultAgent, "async_recognize_intent", _called)
    # Конвейер уже поднял разговор. Убираем указатель встроенного агента.
    manager = get_agent_manager(hass)
    assert manager.default_agent is not None
    manager.default_agent = None
    client = await hass_client()
    phrase = "включи лампу"
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    await assert_empty_500(response)
    assert calls == []
    own = own_messages(caplog)
    assert_no_route_literals(own)
    assert "_MissingBuiltinAgentError" in "\n".join(own)
    assert phrase in "\n".join(own_messages(caplog, level=logging.DEBUG))
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_method_exceptions_are_empty_500(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-N05: исключение каждого метода — пустой 500 без текста исключения."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_builtin_agent(hass)
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    intent_calls: list[str] = []

    async def _trigger_failed(
        self: DefaultAgent, user_input: ConversationInput
    ) -> None:
        del self, user_input
        raise RuntimeError("секрет-триггера")

    async def _intent_count(
        self: DefaultAgent,
        user_input: ConversationInput,
        strict_intents_only: bool = False,
    ) -> Any:
        del self, user_input, strict_intents_only
        intent_calls.append("intent")
        return None

    monkeypatch.setattr(
        DefaultAgent, "async_recognize_sentence_trigger", _trigger_failed
    )
    monkeypatch.setattr(DefaultAgent, "async_recognize_intent", _intent_count)
    caplog.clear()
    triggered = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    await assert_empty_500(triggered)
    own = "\n".join(own_messages(caplog))
    assert "секрет-триггера" not in own
    assert_no_route_literals(own_messages(caplog))
    assert "RuntimeError" in own
    assert intent_calls == []
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment

    async def _trigger_miss(self: DefaultAgent, user_input: ConversationInput) -> None:
        del self, user_input
        return None

    async def _intent_failed(
        self: DefaultAgent,
        user_input: ConversationInput,
        strict_intents_only: bool = False,
    ) -> None:
        del self, user_input, strict_intents_only
        raise RuntimeError("секрет-намерения")

    monkeypatch.setattr(DefaultAgent, "async_recognize_sentence_trigger", _trigger_miss)
    monkeypatch.setattr(DefaultAgent, "async_recognize_intent", _intent_failed)
    caplog.clear()
    intended = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    await assert_empty_500(intended)
    own = "\n".join(own_messages(caplog))
    assert "секрет-намерения" not in own
    assert_no_route_literals(own_messages(caplog))
    assert "RuntimeError" in own
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


@pytest.mark.parametrize(
    ("method_name", "exc_type"),
    [
        ("async_recognize_sentence_trigger", asyncio.CancelledError),
        ("async_recognize_sentence_trigger", KeyboardInterrupt),
        ("async_recognize_sentence_trigger", SystemExit),
        ("async_recognize_sentence_trigger", GeneratorExit),
        ("async_recognize_intent", asyncio.CancelledError),
        ("async_recognize_intent", KeyboardInterrupt),
        ("async_recognize_intent", SystemExit),
        ("async_recognize_intent", GeneratorExit),
    ],
)
async def test_route_reraises_passthrough(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    exc_type: type[BaseException],
) -> None:
    """AC-N07: _route не превращает четыре базовых исключения в пустой 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise exc_type

    monkeypatch.setattr(DefaultAgent, method_name, _boom)
    turn = _TurnLog()
    with pytest.raises(exc_type):
        await ResponsesView()._route(hass, "включи лампу", turn)
    assert turn.route is None
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


@pytest.mark.parametrize(
    "method_name",
    ["async_recognize_sentence_trigger", "async_recognize_intent"],
)
async def test_client_cancellation_is_not_empty_500(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """AC-N07: отмена уходит клиенту, пустого HTTP 500 нет."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_builtin_agent(hass)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(DefaultAgent, method_name, _boom)
    client = await hass_client()
    with pytest.raises((asyncio.CancelledError, ServerDisconnectedError)):
        await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, "включи лампу", history=False),
        )
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_classify_without_token_is_401(
    hass: HomeAssistant,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-AC-01: без токена Home Assistant отвечает 401 до ветки и конвейера."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    seen = watch_route(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    client = await aiohttp_client(hass.http.app)
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    assert response.status == 401
    assert seen == []


async def test_token_is_not_stored_on_classify(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-AC-01b: токен ветки не попадает в запись, настройки и runtime_data."""
    del monkeypatch
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(hass, [automation_item("Лампа", "включи лампу")])
    client = await hass_client(hass_read_only_access_token)
    phrase = "включи лампу"
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    assert await read_route(response) == "llm"
    stored = json.dumps(
        {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "runtime": {
                "conversation_id": entry.runtime_data.conversation_id,
                "last_success": entry.runtime_data.last_success,
                "pipeline_unavailable": entry.runtime_data.pipeline_unavailable,
            },
        },
        ensure_ascii=False,
        default=str,
    )
    assert hass_read_only_access_token not in stored
    assert hass_access_token not in stored
    assert phrase not in stored
    assert "Bearer" not in stored
    assert "agent_id" not in entry.data
    assert "agent_id" not in entry.options


async def test_classify_does_not_read_body_dialogue_or_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-AC-03: на ветке не читаются диалог и конвейер из тела."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_setup_automations(hass, [automation_item("Лампа", "включи лампу")])

    async def _guarded(request: Any) -> object:
        loaded = await async_load_json_body(request)
        assert isinstance(loaded, dict)
        return _Guard(loaded)

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.async_load_json_body",
        _guarded,
    )
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    assert await read_route(response) == "llm"
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_models_do_not_classify_or_log_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GET /models не запускает конвейер и маршрут и не пишет реплику."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    seen = watch_route(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    response = await client.get(MODELS_URL)
    assert response.status == 200
    payload = await response.json()
    assert {"id": entry.entry_id, "name": entry.title} in payload["data"]
    assert own_messages(caplog, level=logging.DEBUG) == []
    assert seen == []
