"""Адрес Responses и список записей."""

import asyncio
import json
from datetime import timedelta
from typing import Any, cast

import pytest
from aiohttp import ClientSession
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.quasar_assist.const import (
    DOMAIN,
    MAX_REQUEST_BODY_BYTES,
    MODELS_URL,
    MODELS_VIEW_NAME,
    PHRASE_EMPTY,
    PHRASE_FAILED,
    PHRASE_NOT_FOUND,
    RESPONSES_URL,
    RESPONSES_VIEW_NAME,
)
from custom_components.quasar_assist.http import (
    ModelsView,
    ResponsesView,
    async_call_assist,
    parse_body,
    plain_speech,
)
from custom_components.quasar_assist.runtime import QuasarAssistRuntimeData
from tests.common import (
    Clock,
    ConverseMock,
    assert_completed,
    assert_same_entry,
    async_add_entry,
    async_prepare_conversation,
    async_register_agent,
    build_result,
    patch_clock,
    patch_converse,
    responses_body,
)


def test_view_names_are_stable() -> None:
    """Имя представления не собирается заново на каждый запрос."""
    assert ResponsesView.url == RESPONSES_URL
    assert ModelsView.url == MODELS_URL
    assert ResponsesView.name == RESPONSES_VIEW_NAME
    assert ModelsView.name == MODELS_VIEW_NAME
    assert ResponsesView.requires_auth is True
    assert ModelsView.requires_auth is True
    assert isinstance(ResponsesView.name, str)
    assert ResponsesView.name != ""


async def test_phrase_is_sent_unchanged(
    hass: HomeAssistant,
    hass_client: Any,
    hass_admin_user: Any,
    monkeypatch: Any,
) -> None:
    """AC-01: последняя фраза уходит как есть, шум запроса в Assist не попадает."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client: ClientSession = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет"),
    )
    assert response.status == 200
    assert_completed(await response.json(), "Включила свет")
    assert len(mock.calls) == 1
    text, conversation_id, context, kwargs = mock.calls[0]
    assert text == "включи свет"
    assert conversation_id is None
    assert kwargs == {"agent_id": conversation.HOME_ASSISTANT_AGENT}
    assert "language" not in kwargs
    assert isinstance(context, Context)
    assert context.user_id == hass_admin_user.id
    runtime: QuasarAssistRuntimeData = entry.runtime_data
    assert runtime.conversation_id == "conv-1"
    assert runtime.last_success is not None


async def test_prefixed_phrase_is_not_trimmed(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-N05: обращение, которое мост не срезал, уходит дословно."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "Львовна, включи свет", noise=False),
    )
    assert response.status == 200
    assert mock.calls[0][0] == "Львовна, включи свет"


async def test_whitespace_phrase_is_not_trimmed(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """Пробелы во фразе не считаются пустотой и не срезаются."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, " включи свет ", noise=False),
    )
    assert response.status == 200
    assert mock.calls[0][0] == " включи свет "


async def test_error_type_with_speech_extends_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-02: непустая речь при ошибочном типе ответа остаётся успехом."""
    mock = ConverseMock()
    mock.error_type = True
    mock.speech = "Включила свет"
    patch_converse(monkeypatch, mock)
    clock = Clock(dt_util.utcnow())
    patch_clock(monkeypatch, clock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT, ttl=90)
    client = await hass_client()
    first = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет"),
    )
    assert first.status == 200
    assert_completed(await first.json(), "Включила свет")
    clock.moment = clock.moment + timedelta(seconds=10)
    second = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "А в спальне", noise=False),
    )
    assert second.status == 200
    assert mock.calls[1][0] == "А в спальне"
    assert mock.calls[1][1] == "conv-1"
    runtime: QuasarAssistRuntimeData = entry.runtime_data
    assert runtime.conversation_id == "conv-2"
    assert runtime.last_success == clock.moment


async def test_two_entries_do_not_share_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-03: разные записи вызывают своих ассистентов и не делят диалог."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    other = await async_register_agent(hass, "Кухня")
    bedroom = await async_add_entry(
        hass,
        conversation.HOME_ASSISTANT_AGENT,
        title="Спальня",
        entry_id="entry-bedroom",
    )
    kitchen = await async_add_entry(
        hass,
        other,
        title="Кухня",
        entry_id="entry-kitchen",
    )
    client = await hass_client()
    await client.post(
        RESPONSES_URL,
        json=responses_body(bedroom.entry_id, "включи свет", noise=False),
    )
    await client.post(
        RESPONSES_URL,
        json=responses_body(kitchen.entry_id, "включи чайник", noise=False),
    )
    await client.post(
        RESPONSES_URL,
        json=responses_body(bedroom.entry_id, "А в спальне", noise=False),
    )
    assert mock.calls[0][3]["agent_id"] == conversation.HOME_ASSISTANT_AGENT
    assert mock.calls[1][3]["agent_id"] == other
    assert mock.calls[0][1] is None
    assert mock.calls[1][1] is None
    assert mock.calls[2][1] == "conv-1"
    assert mock.calls[2][1] != mock.calls[1][1]


async def test_timer_continues_any_phrase_until_it_expires(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """Таймер продолжает любую фразу и после срока начинает новую."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    clock = Clock(dt_util.utcnow())
    patch_clock(monkeypatch, clock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT, ttl=90)
    client = await hass_client()

    async def say(text: str) -> None:
        response = await client.post(
            RESPONSES_URL,
            json=responses_body(entry.entry_id, text, noise=False),
        )
        assert response.status == 200

    await say("включи свет")
    clock.moment = clock.moment + timedelta(seconds=89)
    await say("А в спальне")
    await say("включи телевизор")
    await say("да")
    await say("нет")
    await say("а выключи")
    continued = [call[1] for call in mock.calls[1:]]
    assert continued == [f"conv-{index}" for index in range(1, 6)]
    assert [call[0] for call in mock.calls[1:]] == [
        "А в спальне",
        "включи телевизор",
        "да",
        "нет",
        "а выключи",
    ]

    clock.moment = mock_last(entry) + timedelta(seconds=90)
    await say("да")
    assert mock.calls[-1][0] == "да"
    assert mock.calls[-1][1] is None


def mock_last(entry: Any) -> Any:
    """Момент последнего успеха записи."""
    return entry.runtime_data.last_success


async def test_models_list_entries_without_assist(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-05: список из config entries, порядок по id, таймеры не меняются."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    named = await async_add_entry(
        hass,
        conversation.HOME_ASSISTANT_AGENT,
        title="Спальня",
        entry_id="entry-b",
    )
    untitled = await async_add_entry(
        hass,
        conversation.HOME_ASSISTANT_AGENT,
        title="",
        entry_id="entry-a",
    )
    named.runtime_data.conversation_id = "keep"
    moment = dt_util.utcnow()
    named.runtime_data.last_success = moment
    client = await hass_client()
    response = await client.get(MODELS_URL)
    assert response.status == 200
    payload = await response.json()
    assert payload == {
        "data": [
            {"id": "entry-a", "name": "entry-a"},
            {"id": "entry-b", "name": "Спальня"},
        ]
    }
    assert mock.calls == []
    assert named.runtime_data.conversation_id == "keep"
    assert named.runtime_data.last_success == moment
    assert untitled.runtime_data.conversation_id is None


async def test_models_empty(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """Пустой каталог, если записей нет. Assist не вызывается."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    await async_prepare_conversation(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(MODELS_URL)
    assert response.status == 200
    assert await response.json() == {"data": []}
    assert mock.calls == []


async def test_no_outgoing_language_model(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-07: запрос обрабатывается без исходящего вызова языковой модели."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    posted = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    listed = await client.get(MODELS_URL)
    assert posted.status == 200
    assert listed.status == 200


async def test_unknown_model_and_order(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """Сначала чужая форма, затем неизвестная запись, затем пустая фраза."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    other = await async_register_agent(hass, "Чужая")
    entry = await async_add_entry(
        hass,
        conversation.HOME_ASSISTANT_AGENT,
        title="Спальня",
    )
    entry.runtime_data.conversation_id = "live"
    moment = dt_util.utcnow()
    entry.runtime_data.last_success = moment
    client = await hass_client()

    foreign = await async_add_entry(hass, other, title="Не наш домен")
    # Запись другого домена уже есть как openai_conversation. Её id не наш.
    bad_form = await client.post(
        RESPONSES_URL,
        json={"model": "nope", "input": {"role": "user", "content": "включи свет"}},
    )
    assert bad_form.status == 400
    assert mock.calls == []

    unknown = await client.post(
        RESPONSES_URL,
        json=responses_body("", "", noise=False),
    )
    assert unknown.status == 200
    assert_completed(await unknown.json(), PHRASE_NOT_FOUND)

    other_domain = await client.post(
        RESPONSES_URL,
        json=responses_body(other, "включи свет", noise=False),
    )
    assert other_domain.status == 200
    assert_completed(await other_domain.json(), PHRASE_NOT_FOUND)

    missing_model = await client.post(
        RESPONSES_URL,
        json={"input": [{"role": "user", "content": "включи свет"}]},
    )
    assert missing_model.status == 200
    assert_completed(await missing_model.json(), PHRASE_NOT_FOUND)

    empty = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "", noise=False),
    )
    assert empty.status == 200
    assert_completed(await empty.json(), PHRASE_EMPTY)
    assert entry.runtime_data.conversation_id == "live"
    assert entry.runtime_data.last_success == moment

    absent = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, None, noise=True),
    )
    assert absent.status == 200
    assert_completed(await absent.json(), PHRASE_EMPTY)
    assert entry.runtime_data.conversation_id == "live"
    assert mock.calls == []
    assert foreign.entry_id != entry.entry_id


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "x"},
        {"model": "x", "input": "включи свет"},
        {"model": "x", "input": None},
        {"model": "x", "input": [{"role": "user", "content": {"type": "text"}}]},
        {"model": "x", "input": [{"role": "user", "content": ["включи свет"]}]},
        {"model": "x", "input": [{"role": "user", "content": 1}]},
        {"model": "x", "input": [{"role": "user"}]},
        {
            "model": "x",
            "input": [
                {"role": "user", "content": "включи свет"},
                {"role": "user", "content": 1},
            ],
        },
        ["not-an-object"],
    ],
)
async def test_bad_form_is_400(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
    payload: object,
) -> None:
    """QA-I-01: чужая форма отвечает 400 и не вызывает Assist."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    if isinstance(payload, dict) and payload.get("model") == "x":
        payload = {**payload, "model": entry.entry_id}
    response = await client.post(RESPONSES_URL, json=payload)
    assert response.status == 400
    assert mock.calls == []


async def test_invalid_json_is_400(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """Неразобранный JSON не вызывает Assist."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        data=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 400
    assert mock.calls == []


async def test_empty_speech_and_exception_close_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-N06: пустая речь и исключение закрывают диалог."""
    mock = ConverseMock()
    mock.speech = ""
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    entry.runtime_data.conversation_id = "live"
    entry.runtime_data.last_success = dt_util.utcnow()
    client = await hass_client()
    empty = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert empty.status == 200
    assert_completed(await empty.json(), PHRASE_FAILED)
    assert len(mock.calls) == 1
    assert entry.runtime_data.conversation_id is None
    assert entry.runtime_data.last_success is None

    mock.speech = "Включила свет"
    mock.exc = RuntimeError("включи свет")
    entry.runtime_data.conversation_id = "live"
    entry.runtime_data.last_success = dt_util.utcnow()
    failed = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert failed.status == 200
    assert_completed(await failed.json(), PHRASE_FAILED)
    assert len(mock.calls) == 2
    assert entry.runtime_data.conversation_id is None


async def test_failed_start_does_not_call_assist(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-N08: ассистента нет, непустая фраза закрывает диалог без вызова."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, "missing.agent")
    assert_same_entry(hass.config_entries.async_get_entry(entry.entry_id), entry)
    assert getattr(entry, "runtime_data", None) is None
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    assert mock.calls == []
    assert getattr(entry, "runtime_data", None) is None


async def test_unauthorized_is_401(
    hass: HomeAssistant,
    aiohttp_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-N07: без токена Home Assistant отвечает 401 до Assist."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await aiohttp_client(hass.http.app)
    posted = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    listed = await client.get(MODELS_URL)
    assert posted.status == 401
    assert listed.status == 401
    assert mock.calls == []


async def test_non_admin_token_is_accepted_and_not_stored(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    hass_read_only_user: Any,
    hass_read_only_access_token: str,
    monkeypatch: Any,
) -> None:
    """Действительный токен не проверяется по роли и не пишется в запись."""
    assert not hass_read_only_user.is_admin
    token = hass_read_only_access_token
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client(token)
    phrase = "включи свет"
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, phrase, noise=False),
    )
    assert response.status == 200
    assert mock.calls[0][2].user_id == hass_read_only_user.id
    stored = json.dumps(
        {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "conversation_id": entry.runtime_data.conversation_id,
        },
        ensure_ascii=False,
        default=str,
    )
    assert token not in stored
    assert hass_access_token not in stored
    assert phrase not in stored
    assert "Bearer" not in stored


async def test_models_accepts_token_without_admin(
    hass: HomeAssistant,
    hass_client: Any,
    hass_read_only_user: Any,
    hass_read_only_access_token: str,
    monkeypatch: Any,
) -> None:
    """SEC-AC-01: токен без прав администратора читает список, Assist молчит."""
    assert not hass_read_only_user.is_admin
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client(hass_read_only_access_token)
    response = await client.get(MODELS_URL)
    assert response.status == 200
    payload = await response.json()
    assert {"id": entry.entry_id, "name": entry.title} in payload["data"]
    assert mock.calls == []


async def test_oversized_body_is_400_without_assist(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """Тело больше предела отклоняется до Assist и не попадает в журнал."""
    assert MAX_REQUEST_BODY_BYTES >= 1024 * 1024
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    marker = "секретная-реплика-лимита"
    phrase = marker + ("x" * MAX_REQUEST_BODY_BYTES)
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    rejected = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, phrase, noise=False),
    )
    assert rejected.status == 400
    assert mock.calls == []
    own = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    )
    assert marker not in own
    assert hass_access_token not in own

    accepted = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert accepted.status == 200
    assert mock.calls[0][0] == "включи свет"


async def test_oversized_chunked_body_is_400_without_assist(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """Тело больше предела без Content-Length не доходит до Assist и журнала."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    marker = "секретная-реплика-без-длины"
    phrase = marker + ("x" * MAX_REQUEST_BODY_BYTES)
    payload = json.dumps(
        responses_body(entry.entry_id, phrase, noise=False),
    ).encode()
    assert len(payload) > MAX_REQUEST_BODY_BYTES
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    rejected = await client.post(
        RESPONSES_URL,
        data=payload,
        chunked=True,
        headers={"Content-Type": "application/json"},
    )
    assert rejected.status == 400
    assert rejected.request_info.headers.get("Content-Length") is None
    assert mock.calls == []
    own = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    )
    assert marker not in own
    assert hass_access_token not in own


async def test_logs_do_not_contain_phrase_or_token(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """QA-P-01: журнал интеграции не содержит токен и реплику."""
    mock = ConverseMock()
    mock.exc = RuntimeError("секретная реплика")
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "секретная реплика", noise=False),
    )
    own = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    )
    assert "секретная реплика" not in own
    assert hass_access_token not in own
    assert "Bearer" not in own
    assert "authorization" not in own.lower()


async def test_call_assist_reraises_cancellation(monkeypatch: Any) -> None:
    """Отмена задачи не превращается в фразу о неудаче."""

    async def cancelled(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "custom_components.quasar_assist.http.conversation.async_converse",
        cancelled,
    )
    try:
        await async_call_assist(
            hass=None,  # type: ignore[arg-type]
            text="включи свет",
            conversation_id=None,
            context=Context(),
            agent_id="conversation.home_assistant",
        )
    except asyncio.CancelledError:
        return
    raise AssertionError("отмена должна всплыть")


def test_parse_and_speech_edges() -> None:
    """Края разбора тела и пустой речи."""
    assert parse_body(["x"]) is None
    parsed = parse_body(
        {
            "model": 5,
            "input": [
                {"role": "user", "content": {"bad": True}},
                {"role": "user", "content": "включи свет"},
            ],
        }
    )
    assert parsed is not None
    assert parsed.model == ""
    assert parsed.text == "включи свет"
    result = build_result("", None)
    assert plain_speech(result) == ""
    result.response.speech = cast(dict[str, dict[str, Any]], "нет")
    assert plain_speech(result) == ""
    result.response.speech = cast(dict[str, dict[str, Any]], {"plain": "нет"})
    assert plain_speech(result) == ""
    result.response.speech = cast(
        dict[str, dict[str, Any]],
        {"plain": {"speech": 1}},
    )
    assert plain_speech(result) == ""
