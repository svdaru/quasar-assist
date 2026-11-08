"""Создание и перенастройка записи."""

from datetime import timedelta
from typing import Any

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.quasar_assist.agent import agent_is_available
from custom_components.quasar_assist.config_flow import parse_ttl
from custom_components.quasar_assist.const import (
    ABORT_NO_ASSISTANTS,
    CONF_AGENT_ID,
    CONF_CONVERSATION_TTL,
    DEFAULT_CONVERSATION_TTL,
    DOMAIN,
    ERROR_ASSISTANT_NOT_FOUND,
    ERROR_INVALID_TTL,
)
from tests.common import (
    Clock,
    ConverseMock,
    assert_completed,
    async_prepare_conversation,
    async_register_agent,
    patch_clock,
    patch_converse,
    responses_body,
)


def test_parse_ttl_edges() -> None:
    """Срок принимается только как целое число секунд от 1."""
    assert parse_ttl(90) == 90
    assert parse_ttl(90.0) == 90
    assert parse_ttl(1.5) is None
    assert parse_ttl(True) is None
    assert parse_ttl(0) is None
    assert parse_ttl(-5) is None
    assert parse_ttl("90") is None
    assert parse_ttl(None) is None


async def test_abort_without_assistants(
    hass: HomeAssistant,
    monkeypatch: Any,
) -> None:
    """Без ассистента запись не создаётся."""
    await async_prepare_conversation(hass)
    monkeypatch.setattr(
        "custom_components.quasar_assist.config_flow.async_list_agents",
        lambda _hass: {},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == ABORT_NO_ASSISTANTS
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_create_entry_and_allow_same_assistant(hass: HomeAssistant) -> None:
    """AC-06: выбор из списка, срок 90, тот же ассистент можно выбрать снова."""
    await async_prepare_conversation(hass)
    agent_id = conversation.HOME_ASSISTANT_AGENT

    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert first["type"] == "form"
    created = await hass.config_entries.flow.async_configure(
        first["flow_id"],
        {CONF_AGENT_ID: agent_id},
    )
    assert created["type"] == "create_entry"
    assert created["data"] == {
        CONF_AGENT_ID: agent_id,
        CONF_CONVERSATION_TTL: DEFAULT_CONVERSATION_TTL,
    }
    assert created["result"].unique_id is None
    assert created["title"]

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    again = await hass.config_entries.flow.async_configure(
        second["flow_id"],
        {CONF_AGENT_ID: agent_id, CONF_CONVERSATION_TTL: 30},
    )
    assert again["type"] == "create_entry"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert {entry.data[CONF_AGENT_ID] for entry in entries} == {agent_id}
    assert {entry.entry_id for entry in entries} == {
        created["result"].entry_id,
        again["result"].entry_id,
    }
    assert entries[0].runtime_data.conversation_id is None


async def test_rejects_unknown_assistant(
    hass: HomeAssistant,
    monkeypatch: Any,
) -> None:
    """Ассистент проверяется до создания записи."""
    await async_prepare_conversation(hass)
    monkeypatch.setattr(
        "custom_components.quasar_assist.config_flow.agent_is_available",
        lambda *_args, **_kwargs: False,
    )
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            CONF_AGENT_ID: conversation.HOME_ASSISTANT_AGENT,
            CONF_CONVERSATION_TTL: 90,
        },
    )
    assert result["type"] == "form"
    errors = result["errors"]
    assert errors is not None
    assert errors[CONF_AGENT_ID] == ERROR_ASSISTANT_NOT_FOUND
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert agent_is_available(hass, "missing.agent") is False


async def test_rejects_invalid_ttl(hass: HomeAssistant) -> None:
    """Некорректный срок не создаёт запись и берётся из перевода ошибки."""
    await async_prepare_conversation(hass)
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            CONF_AGENT_ID: conversation.HOME_ASSISTANT_AGENT,
            CONF_CONVERSATION_TTL: 1.5,
        },
    )
    assert result["type"] == "form"
    errors = result["errors"]
    assert errors is not None
    assert errors[CONF_CONVERSATION_TTL] == ERROR_INVALID_TTL
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_reconfigure_keeps_entry(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
) -> None:
    """AC-09: смена ассистента и срока не удаляет запись и ограничивает таймер."""
    other = await async_register_agent(hass, "Кухня")
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    clock = Clock(dt_util.utcnow())
    patch_clock(monkeypatch, clock)

    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    created = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            CONF_AGENT_ID: conversation.HOME_ASSISTANT_AGENT,
            CONF_CONVERSATION_TTL: 90,
        },
    )
    entry = created["result"]
    reconfigure = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert reconfigure["type"] == "form"
    updated = await hass.config_entries.flow.async_configure(
        reconfigure["flow_id"],
        {CONF_AGENT_ID: other, CONF_CONVERSATION_TTL: 15},
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    assert entry.data[CONF_AGENT_ID] == other
    assert entry.data[CONF_CONVERSATION_TTL] == 15
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry

    client = await hass_client()
    first = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert first.status == 200
    assert_completed(await first.json(), "Включила свет")
    assert mock.calls[0][3]["agent_id"] == other

    clock.moment = clock.moment + timedelta(seconds=15)
    await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "ещё", noise=False),
    )
    assert mock.calls[1][1] is None
