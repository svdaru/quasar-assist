"""Ветка маршрута classify и отладочная строка хода."""

import asyncio
import json
import logging
from typing import Any

import pytest
from aiohttp.client_exceptions import ServerDisconnectedError
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from homeassistant.components.automation import DATA_COMPONENT, AutomationEntity
from homeassistant.components.conversation.agent_manager import AgentManager
from homeassistant.components.conversation.default_agent import DefaultAgent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.quasar_assist.classify import (
    commands_in_config,
    conversation_commands,
    phrase_matches,
)
from custom_components.quasar_assist.const import (
    MODELS_URL,
    PHRASE_EMPTY,
    PHRASE_NOT_FOUND,
    RESPONSES_URL,
)
from custom_components.quasar_assist.diagnostics import (
    async_get_config_entry_diagnostics,
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


def test_commands_in_config_filters_triggers() -> None:
    """В выборке оба ключа, одна обёртка triggers и только включённые команды."""
    assert commands_in_config(
        {"trigger": {"platform": "conversation", "command": "одна"}}
    ) == ["одна"]
    assert commands_in_config(
        {
            "trigger": [{"platform": "conversation", "command": "старый"}],
            "triggers": [{"trigger": "conversation", "command": "новый"}],
        }
    ) == ["старый", "новый"]
    assert commands_in_config(
        {"triggers": [{"trigger": "conversation", "command": ["а", "б"]}]}
    ) == ["а", "б"]
    assert commands_in_config(
        {
            "triggers": [
                {
                    "platform": "conversation",
                    "command": "скрытая",
                    "enabled": False,
                },
                {"platform": "conversation", "command": "явная", "enabled": True},
                {"platform": "state", "command": "не та"},
                {"platform": "conversation", "command": ["да", 1, None]},
                "не словарь",
                {"platform": "conversation"},
            ]
        }
    ) == ["явная", "да"]
    assert commands_in_config(
        {
            "triggers": [
                {
                    "triggers": [
                        {"platform": "conversation", "command": "внутри"},
                    ]
                }
            ]
        }
    ) == ["внутри"]
    assert commands_in_config(
        {
            "trigger": [
                {
                    "triggers": [
                        {"trigger": "conversation", "command": "старая"},
                    ]
                }
            ]
        }
    ) == ["старая"]
    assert commands_in_config(
        {
            "triggers": [
                {
                    "triggers": {
                        "platform": "conversation",
                        "command": "скаляр",
                    }
                }
            ]
        }
    ) == ["скаляр"]
    assert (
        commands_in_config(
            {
                "triggers": [
                    {
                        "triggers": [
                            {"platform": "conversation", "command": "сбоку"},
                        ],
                        "id": "extra",
                    }
                ]
            }
        )
        == []
    )
    deep = {
        "triggers": [
            {
                "triggers": [
                    {
                        "triggers": [
                            {
                                "platform": "conversation",
                                "command": "глубокая",
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert commands_in_config(deep) == []
    assert commands_in_config(
        {
            "triggers": [
                {
                    "platform": "conversation",
                    "command": "строка",
                    "enabled": "не шаблон",
                }
            ]
        }
    ) == ["строка"]
    assert (
        commands_in_config(
            {
                "triggers": [
                    {
                        "platform": "conversation",
                        "command": "шаблон",
                        "enabled": "{{ false }}",
                    }
                ]
            }
        )
        == []
    )
    assert commands_in_config({"other": []}) == []


def test_commands_in_config_unwraps_dict_value() -> None:
    """Значение-словарь trigger и triggers разворачивается один раз."""
    inner = {"platform": "conversation", "command": "внутри"}
    assert commands_in_config({"triggers": {"triggers": [inner]}}) == ["внутри"]
    assert commands_in_config({"trigger": {"triggers": [inner]}}) == ["внутри"]
    assert commands_in_config(
        {"triggers": {"trigger": "conversation", "command": "поле"}}
    ) == ["поле"]
    assert commands_in_config({"triggers": {"triggers": [{"triggers": [inner]}]}}) == []


def test_enabled_string_uses_boolean_before_template() -> None:
    """Строка enabled сначала boolean, отказ — статический шаблон."""

    def item(enabled: str) -> dict[str, Any]:
        return {
            "triggers": [
                {
                    "platform": "conversation",
                    "command": "фраза",
                    "enabled": enabled,
                }
            ]
        }

    assert commands_in_config(item("false")) == []
    assert commands_in_config(item("off")) == []
    assert commands_in_config(item(" OFF ")) == []
    assert commands_in_config(item("no")) == []
    assert commands_in_config(item("0")) == []
    assert commands_in_config(item("disable")) == []
    assert commands_in_config(item("true")) == ["фраза"]
    assert commands_in_config(item("лампа")) == ["фраза"]
    assert commands_in_config(item("")) == []
    assert commands_in_config(item("   ")) == []


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


async def async_setup_automations(
    hass: HomeAssistant, items: list[dict[str, Any]]
) -> None:
    """Поднять автоматизации. Неудачная схема роняет тест."""
    assert await async_setup_component(hass, "automation", {"automation": items})
    await hass.async_block_till_done()


def listen(hass: HomeAssistant) -> list[Any]:
    """События действия автоматизации после этой подписки."""
    found: list[Any] = []
    hass.bus.async_listen(PROBE_EVENT, lambda event: found.append(event))
    return found


def forbid_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запуск конвейера из адреса — ошибка теста."""

    async def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("конвейер запущен")

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.async_run_pipeline",
        _forbidden,
    )


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


async def test_match_returns_llm_without_pipeline_or_action(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-01 и AC-06: совпадение даёт llm, диалог и автоматизация не тронуты."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_setup_automations(
        hass,
        [automation_item("Лампа", "включи лампу")],
    )
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

    def _register(self: object, *_args: object, **_kwargs: object) -> None:
        del self
        raise AssertionError("register_trigger")

    async def _recognize(self: object, *_args: object, **_kwargs: object) -> None:
        del self
        raise AssertionError("async_recognize_sentence_trigger")

    async def _trigger(self: object, *_args: object, **_kwargs: object) -> None:
        del self
        raise AssertionError("async_trigger")

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.refresh_pipeline",
        _refresh,
    )
    monkeypatch.setattr("custom_components.quasar_assist.http.close_dialogue", _close)
    monkeypatch.setattr(
        "custom_components.quasar_assist.http.remember_success", _remember
    )
    monkeypatch.setattr("custom_components.quasar_assist.http.hold_chat_session", _hold)
    monkeypatch.setattr(AgentManager, "register_trigger", _register)
    monkeypatch.setattr(DefaultAgent, "async_recognize_sentence_trigger", _recognize)
    monkeypatch.setattr(AutomationEntity, "async_trigger", _trigger)
    fired = listen(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу"),
    )
    assert await read_route(response) == "llm"
    assert capture.inputs == []
    assert fired == []
    assert calls == []
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_no_match_returns_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-02 и AC-06: нет подходящей команды — yandex, не HTTP 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    client = await hass_client()

    seen: list[str] = []

    def _spy(_hass: HomeAssistant, text: str, _sentences: list[str]) -> bool:
        seen.append(text)
        raise AssertionError("распознаватель")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        _spy,
    )
    missing = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "нет такой команды"),
    )
    assert await read_route(missing) == "yandex"
    assert seen == []

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        phrase_matches,
    )
    await async_setup_automations(
        hass,
        [automation_item("Другая", "совсем другая фраза")],
    )
    other = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "нет такой команды"),
    )
    assert await read_route(other) == "yandex"
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment

    for entity in hass.data[DATA_COMPONENT].entities:
        entity.raw_config = None
    cleared = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "нет такой команды"),
    )
    assert await read_route(cleared) == "yandex"


async def test_optional_fragment_and_slot_match(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03: необязательный фрагмент и подстановка дают llm без запуска."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
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


async def test_disabled_off_and_script_do_not_match(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-04: выключенная автоматизация, выключенный триггер и сценарий — yandex."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    command = "общая команда"
    await async_setup_automations(
        hass,
        [
            automation_item("Выключена", command, initial_state=False),
            {
                "alias": "Смешанная",
                "triggers": [
                    {"platform": "conversation", "command": "другая команда"},
                    {
                        "platform": "conversation",
                        "command": command,
                        "enabled": False,
                    },
                ],
                "actions": [{"event": PROBE_EVENT}],
            },
        ],
    )
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "probe_script": {
                    "alias": command,
                    "sequence": [{"event": PROBE_EVENT}],
                }
            }
        },
    )
    await hass.async_block_till_done()
    fired = listen(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, command, history=False),
    )
    assert await read_route(response) == "yandex"
    assert fired == []


async def test_second_command_in_the_list_matches(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-05: любая команда одного триггера даёт llm."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [
            automation_item(
                "Список",
                ["первая команда", "вторая команда"],
            )
        ],
    )
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "вторая команда", history=False),
    )
    assert await read_route(response) == "llm"


async def test_without_classify_runs_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-07: без ветки непустая фраза идёт в конвейер, ответ — речь."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = "Включила свет"
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(
            entry.entry_id,
            "включи свет",
            system="Инструкции инструментов моста",
            history=False,
        ),
    )
    assert response.status == 200
    payload = await response.json()
    speech = intent_speech(capture.events[0])
    assert_completed(payload, speech)
    assert speech == "Включила свет"
    assert json.dumps(payload).find('{"route"') == -1
    assert len(capture.inputs) == 1
    assert capture.inputs[0].run.start_stage.name == "INTENT"
    assert capture.inputs[0].run.end_stage.name == "INTENT"
    assert entry.runtime_data.conversation_id is not None
    assert entry.runtime_data.last_success is not None


async def test_missing_pipeline_still_classifies(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-08: пропавший конвейер и пустой runtime_data не мешают маршруту."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    entry.runtime_data.pipeline_unavailable = False
    await async_setup_automations(
        hass,
        [automation_item("Лампа", "включи лампу")],
    )
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


async def test_classify_debug_line(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-09: одна отладочная строка с репликой и маршрутом, без истории."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = "речь-конвейера-которой-нет"
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [automation_item("Лампа", "включи лампу")],
    )
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
    """AC-10: одна отладочная строка содержит реплику и речь, токена нет."""
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


def responses_without_route(model: str, text: str) -> dict[str, Any]:
    """Обычный запрос конвейера с историей и системным текстом."""
    return classify_body(
        model,
        text,
        system="Инструкции инструментов моста",
        later_system=SYSTEM_SECRET,
    )


async def test_higher_levels_and_diagnostics_hide_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-11: информация, предупреждение, ошибка и диагностика без реплики и речи."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    phrase = "реплика-диагностики"
    pipeline_phrase = "фраза-конвейера"
    speech = "речь-диагностики"
    engine.speech = speech
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [automation_item("Лампа", phrase)],
    )
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


async def test_enumeration_and_recognizer_failures(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-14 и AC-N06: сбой списка или распознавателя — пустой 500, диалог тот же."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    await async_setup_automations(
        hass,
        [automation_item("Лампа", "включи лампу")],
    )
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")

    def _list_failed(_hass: HomeAssistant) -> list[str]:
        raise RuntimeError("секрет-перечисления")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.conversation_commands",
        _list_failed,
    )
    caplog.clear()
    listed = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    await assert_empty_500(listed)
    assert "секрет-перечисления" not in "\n".join(own_messages(caplog))
    assert "RuntimeError" in "\n".join(own_messages(caplog, level=logging.DEBUG))
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.conversation_commands",
        conversation_commands,
    )

    def _match_failed(_hass: HomeAssistant, _text: str, _sentences: list[str]) -> bool:
        raise RuntimeError("секрет-распознавателя")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        _match_failed,
    )
    caplog.clear()
    matched = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    await assert_empty_500(matched)
    assert "секрет-распознавателя" not in "\n".join(own_messages(caplog))
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        phrase_matches,
    )
    for entity in hass.data[DATA_COMPONENT].entities:
        entity.raw_config = {
            "triggers": [{"platform": "conversation", "command": "turn ["}],
        }
    caplog.clear()
    broken = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    await assert_empty_500(broken)
    own = "\n".join(own_messages(caplog))
    assert "turn [" not in own
    assert "Unable to find end" not in own
    assert "ParseError" in "\n".join(own_messages(caplog, level=logging.DEBUG))
    assert "включи лампу" in "\n".join(own_messages(caplog, level=logging.DEBUG))
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_unknown_entry_answers_before_empty_or_classify(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-15 и AC-N02: неизвестная запись — короткая фраза, не маршрут и не 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)

    def _forbidden(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("распознаватель")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        _forbidden,
    )
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


async def test_trigger_and_triggers_keys(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-16: команда в ключе trigger и в ключе triggers даёт llm."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [
            automation_item(
                "Старый",
                "команда старого ключа",
                list_key="trigger",
                type_key="platform",
                action_key="action",
            ),
            automation_item(
                "Новый",
                "команда нового ключа",
                list_key="triggers",
                type_key="trigger",
                action_key="actions",
            ),
        ],
    )
    raws = [entity.raw_config for entity in hass.data[DATA_COMPONENT].entities]
    assert any(isinstance(raw, dict) and "trigger" in raw for raw in raws), raws
    assert any(isinstance(raw, dict) and "triggers" in raw for raw in raws), raws
    fired = listen(hass)
    client = await hass_client()
    for phrase in ("команда старого ключа", "команда нового ключа"):
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, phrase, history=False),
        )
        assert await read_route(response) == "llm"
    assert fired == []


async def test_home_assistant_language_not_pipeline_language(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-17: числовое слово совпадает при языке Home Assistant, не конвейера."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня", language="en")
    stored = async_get_pipeline(hass, pipeline_id)
    assert stored.conversation_language == "en"
    hass.config.language = "en"
    sentence = "pegel {1..30:n}"
    assert phrase_matches(hass, "pegel zwanzig", [sentence]) is False
    hass.config.language = "de"
    assert hass.config.language != stored.conversation_language
    assert phrase_matches(hass, "pegel zwanzig", [sentence]) is True
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [automation_item("Уровень", sentence)],
    )
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(
            entry.entry_id,
            "pegel zwanzig",
            history=False,
            language="ru",
        ),
    )
    assert await read_route(response) == "llm"


async def test_classify_branch_not_enabled(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-18: пробел, нестроковый system и позднее classify не включают ветку."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = "Включила свет"
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
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


async def test_foreign_form_skips_recognizer_and_log(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-N01: чужая форма — 400, без конвейера, распознавателя и тела в журнале."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)

    def _forbidden(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("распознаватель")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        _forbidden,
    )
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
    own = "\n".join(own_messages(caplog))
    assert marker not in own
    assert len(own_messages(caplog, level=logging.DEBUG)) == len(rejected)


async def test_empty_phrase_without_classify_keeps_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N03: пустая фраза без ветки — короткая фраза, диалог и конвейер на месте."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)

    def _forbidden(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("распознаватель")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        _forbidden,
    )
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
    assert entry.runtime_data.conversation_id == "live-dialogue"
    assert entry.runtime_data.last_success == moment


async def test_empty_phrase_with_classify_is_empty_500(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N05: пустая фраза найденной записи на ветке — пустой 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)

    def _forbidden(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("распознаватель")

    monkeypatch.setattr(
        "custom_components.quasar_assist.classify.phrase_matches",
        _forbidden,
    )
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live-dialogue"
    entry.runtime_data.last_success = moment
    client = await hass_client()
    for text in ("", None):
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, text, history=False),
        )
        await assert_empty_500(response)
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

    def _forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("маршрут")

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.match_route",
        _forbidden,
    )
    entry = await async_add_entry(hass, pipeline_id)
    client = await aiohttp_client(hass.http.app)
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, "включи лампу", history=False),
    )
    assert response.status == 401


async def test_token_is_not_stored_on_classify(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-AC-01b: токен ветки не попадает в запись, настройки и runtime_data."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [automation_item("Лампа", "включи лампу")],
    )
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


async def test_models_do_not_classify_or_log_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GET /models не запускает конвейер и распознаватель и не пишет реплику."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)

    def _forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("маршрут")

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.match_route",
        _forbidden,
    )
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    response = await client.get(MODELS_URL)
    assert response.status == 200
    payload = await response.json()
    assert {"id": entry.entry_id, "name": entry.title} in payload["data"]
    assert own_messages(caplog, level=logging.DEBUG) == []


async def test_classify_cancellation_is_not_empty_500(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отмена распознавания не становится пустым HTTP 500."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)

    def _cancel(_hass: HomeAssistant, _text: str) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.match_route",
        _cancel,
    )
    client = await hass_client()
    with pytest.raises((asyncio.CancelledError, ServerDisconnectedError)):
        await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, "включи лампу", history=False),
        )


def entity_is_on(hass: HomeAssistant, alias: str) -> bool:
    """Сущность автоматизации с этим именем включена."""
    for entity in hass.data[DATA_COMPONENT].entities:
        if entity.name == alias and hass.states.is_state(entity.entity_id, "on"):
            return True
    return False


def _raw_enabled(hass: HomeAssistant, alias: str) -> object:
    """Исходная строка enabled из сырого конфига автоматизации."""
    for entity in hass.data[DATA_COMPONENT].entities:
        if entity.name == alias:
            raw_config = entity.raw_config
            if not isinstance(raw_config, dict):
                continue
            triggers = raw_config["triggers"]
            return triggers[0]["enabled"]
    return None


async def _classify_route(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict[str, Any]],
    phrase: str,
    caplog: pytest.LogCaptureFixture | None = None,
) -> tuple[str, list[Any]]:
    """Поднять автоматизации и вернуть маршрут фразы и события действия."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(hass, items)
    if caplog is not None:
        caplog.set_level(logging.DEBUG, logger="custom_components.quasar_assist")
        caplog.clear()
    fired = listen(hass)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase, history=False),
    )
    return await read_route(response), fired


async def test_nested_trigger_wrapper_matches(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Команда только внутри обёртки triggers даёт llm, действие молчит."""
    phrase = "команда внутри обёртки"
    item = {
        "alias": "Обёртка",
        "triggers": [
            {
                "triggers": [
                    {"platform": "conversation", "command": phrase},
                ]
            }
        ],
        "actions": [{"event": PROBE_EVENT}],
    }
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [item],
        phrase,
    )
    assert route == "llm"
    assert fired == []
    assert entity_is_on(hass, "Обёртка")


async def test_triggers_value_wrapper_is_llm(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Значение ключа triggers — словарь-обёртка: llm, действие молчит."""
    phrase = "команда обёртки значением"
    item = {
        "alias": "Обёртка значением",
        "triggers": {
            "triggers": [
                {"platform": "conversation", "command": phrase},
            ]
        },
        "actions": [{"event": PROBE_EVENT}],
    }
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [item],
        phrase,
    )
    assert route == "llm"
    assert fired == []
    assert entity_is_on(hass, "Обёртка значением")


async def test_trigger_wrapper_with_extra_key_is_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Лишний ключ у обёртки не берёт команду, действие молчит."""
    phrase = "команда за лишним ключом"
    item = {
        "alias": "Лишний ключ",
        "triggers": [
            {
                "triggers": [
                    {"platform": "conversation", "command": phrase},
                ],
                "id": "extra",
            }
        ],
        "actions": [{"event": PROBE_EVENT}],
    }
    assert commands_in_config(item) == []
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [item],
        phrase,
    )
    assert route == "yandex"
    assert fired == []


async def test_enabled_template_false_is_yandex(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Шаблон enabled, который рендерится в ложь, даёт yandex."""
    phrase = "команда выключенного шаблона"
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [automation_item("Шаблон ложь", phrase, enabled="{{ false }}")],
        phrase,
    )
    assert route == "yandex"
    assert fired == []
    assert entity_is_on(hass, "Шаблон ложь")


async def test_enabled_boolean_and_static_strings(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """false и off дают yandex, true и нешаблонная строка — llm.

    Совпадение не запускает автоматизацию.
    """
    cases = (
        ("false", "yandex", "Строка false"),
        ("off", "yandex", "Строка off"),
        ("true", "llm", "Строка true"),
        ("лампа", "llm", "Строка лампа"),
    )
    items: list[dict[str, Any]] = []
    expected: list[tuple[str, str, str, str]] = []
    for enabled, route, alias in cases:
        phrase = f"команда enabled {enabled}"
        items.append(automation_item(alias, phrase, enabled=enabled))
        expected.append((phrase, route, alias, enabled))
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(hass, items)
    fired = listen(hass)
    client = await hass_client()
    for phrase, route, alias, enabled in expected:
        assert entity_is_on(hass, alias)
        assert _raw_enabled(hass, alias) == enabled
        response = await client.post(
            RESPONSES_URL,
            json=classify_body(entry.entry_id, phrase, history=False),
        )
        assert await read_route(response) == route
    assert fired == []


async def test_enabled_template_true_is_llm(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Шаблон enabled, который рендерится в истину, даёт llm."""
    phrase = "команда включённого шаблона"
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [automation_item("Шаблон истина", phrase, enabled="{{ true }}")],
        phrase,
    )
    assert route == "llm"
    assert fired == []
    assert entity_is_on(hass, "Шаблон истина")


async def test_enabled_template_error_is_yandex_without_log(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TemplateError в enabled даёт yandex, маркер не пишется, это не HTTP 500."""
    phrase = "команда ошибочного шаблона"
    enabled = "{{ quasar_enabled_marker / 0 }}"
    with pytest.raises(TemplateError) as caught:
        Template(enabled, hass).async_render({"this": None}, limited=True)
    assert "quasar_enabled_marker" in str(caught.value)
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [automation_item("Ошибка шаблона", phrase, enabled=enabled)],
        phrase,
        caplog,
    )
    assert route == "yandex"
    assert fired == []
    assert entity_is_on(hass, "Ошибка шаблона")
    assert "quasar_enabled_marker" not in "\n".join(own_messages(caplog))
    assert "quasar_enabled_marker" not in caplog.text


async def test_trigger_variable_feeds_enabled_template(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Переменная триггера входит в enabled: истина даёт llm, ложь — yandex."""
    phrase_on = "команда включённой переменной"
    phrase_off = "команда выключенной переменной"
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    forbid_pipeline(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    await async_setup_automations(
        hass,
        [
            {
                "alias": "Переменная истина",
                "trigger_variables": {"ready": "{{ true }}"},
                "triggers": [
                    {
                        "platform": "conversation",
                        "command": phrase_on,
                        "enabled": "{{ ready }}",
                    }
                ],
                "actions": [{"event": PROBE_EVENT}],
            },
            {
                "alias": "Переменная ложь",
                "trigger_variables": {"ready": "{{ false }}"},
                "triggers": [
                    {
                        "platform": "conversation",
                        "command": phrase_off,
                        "enabled": "{{ ready }}",
                    }
                ],
                "actions": [{"event": PROBE_EVENT}],
            },
        ],
    )
    assert entity_is_on(hass, "Переменная истина")
    assert entity_is_on(hass, "Переменная ложь")
    fired = listen(hass)
    client = await hass_client()
    opened = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase_on, history=False),
    )
    closed = await client.post(
        RESPONSES_URL,
        json=classify_body(entry.entry_id, phrase_off, history=False),
    )
    assert await read_route(opened) == "llm"
    assert await read_route(closed) == "yandex"
    assert fired == []


async def test_trigger_variable_error_skips_commands(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ошибка переменных триггера не даёт команд и не пишет текст исключения."""
    phrase = "команда сломанной переменной"
    marker = "quasar_var_marker"
    route, fired = await _classify_route(
        hass,
        hass_client,
        monkeypatch,
        [
            {
                "alias": "Сломанная переменная",
                "trigger_variables": {"ready": "{{ quasar_var_marker / 0 }}"},
                "triggers": [
                    {"platform": "conversation", "command": phrase},
                ],
                "actions": [{"event": PROBE_EVENT}],
            }
        ],
        phrase,
        caplog,
    )
    assert route == "yandex"
    assert fired == []
    assert entity_is_on(hass, "Сломанная переменная")
    assert marker not in "\n".join(own_messages(caplog))
    assert marker not in caplog.text


async def test_computed_template_object_enabled(hass: HomeAssistant) -> None:
    """Готовый объект Template включается по результату рендера."""
    command = "команда объекта"
    off = Template("{{ false }}", hass)
    on = Template("{{ true }}", hass)
    broken = Template("{{ quasar_enabled_marker / 0 }}", hass)
    assert (
        commands_in_config(
            {
                "triggers": [
                    {
                        "platform": "conversation",
                        "command": command,
                        "enabled": off,
                    }
                ]
            },
            hass,
        )
        == []
    )
    assert commands_in_config(
        {
            "triggers": [
                {
                    "platform": "conversation",
                    "command": command,
                    "enabled": on,
                }
            ]
        },
        hass,
    ) == [command]
    assert (
        commands_in_config(
            {
                "triggers": [
                    {
                        "platform": "conversation",
                        "command": command,
                        "enabled": broken,
                    }
                ]
            },
            hass,
        )
        == []
    )
