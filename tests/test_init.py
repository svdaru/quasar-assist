"""Загрузка записи, журнал доступности и repair."""

import logging
from typing import Any, cast

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quasar_assist.const import (
    CONF_AGENT_ID,
    CONF_CONVERSATION_TTL,
    DOMAIN,
    PHRASE_FAILED,
)
from custom_components.quasar_assist.http import runtime_of
from custom_components.quasar_assist.runtime import (
    QuasarAssistRuntimeData,
    agent_id_of,
    conversation_ttl,
    issue_id,
    refresh_assistant,
)
from tests.common import (
    ConverseMock,
    assert_completed,
    assert_same_entry,
    async_add_entry,
    patch_converse,
    responses_body,
)


def test_runtime_rejects_foreign_values(hass: HomeAssistant) -> None:
    """Чужой срок и пустое состояние не открывают диалог."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_AGENT_ID: None, CONF_CONVERSATION_TTL: True},
    )
    entry.add_to_hass(hass)
    assert agent_id_of(entry) == ""
    assert conversation_ttl(entry) == 90
    entry.runtime_data = object()
    assert refresh_assistant(hass, entry) is False
    assert runtime_of(entry) is None


def _own(caplog: Any, needle: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
        and needle in record.getMessage()
    ]


def _setup_errors(caplog: Any) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "homeassistant.config_entries"
        and record.levelno >= logging.ERROR
        and "Error setting up entry" in record.getMessage()
    ]


async def test_live_assistant_loads(hass: HomeAssistant) -> None:
    """Живой ассистент загружает запись, repair не поднимается."""
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, QuasarAssistRuntimeData)
    assert entry.runtime_data.assistant_unavailable is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None


async def test_startup_logs_missing_assistant_once(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """Пропавший на старте ассистент пишется один раз исключением и поднимает repair."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    caplog.set_level("ERROR", logger="homeassistant.config_entries")
    entry = await async_add_entry(hass, "missing.agent")
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.SETUP_ERROR
    assert_same_entry(hass.config_entries.async_get_entry(entry.entry_id), entry)
    assert runtime_of(entry) is None
    assert _own(caplog, "недоступен") == []
    assert len(_setup_errors(caplog)) == 1
    assert "no longer available" in _setup_errors(caplog)[0]
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id)) is not None
    )
    assert mock.calls == []

    client = await hass_client()
    response = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    assert mock.calls == []
    assert runtime_of(entry) is None
    assert _own(caplog, "недоступен") == []
    assert len(_setup_errors(caplog)) == 1


async def test_reload_clears_repair_left_from_previous_runtime(
    hass: HomeAssistant,
    caplog: Any,
) -> None:
    """Новая загрузка с чистым runtime снимает уже существующий repair."""
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    entry = await async_add_entry(hass, "missing.agent")
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    hass.config_entries.async_update_entry(
        entry,
        data={
            CONF_AGENT_ID: conversation.HOME_ASSISTANT_AGENT,
            CONF_CONVERSATION_TTL: 90,
        },
    )
    caplog.clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.assistant_unavailable is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None
    assert len(_own(caplog, "снова доступен")) == 1
    assert refresh_assistant(hass, entry) is True
    assert len(_own(caplog, "снова доступен")) == 1


async def test_reconfigure_to_live_assistant_clears_repair(
    hass: HomeAssistant,
    caplog: Any,
) -> None:
    """Перенастройка на живого ассистента снимает repair."""
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    entry = await async_add_entry(hass, "missing.agent")
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    started = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    updated = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            CONF_AGENT_ID: conversation.HOME_ASSISTANT_AGENT,
            CONF_CONVERSATION_TTL: 90,
        },
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None
    assert len(_own(caplog, "снова доступен")) == 1


async def test_unload_keeps_repair_and_remove_clears_it(
    hass: HomeAssistant,
    caplog: Any,
) -> None:
    """Выгрузка repair не снимает, удаление записи снимает."""
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    entry = await async_add_entry(hass, "missing.agent")
    repair = issue_id(entry.entry_id)
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.SETUP_ERROR
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.NOT_LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None


async def test_responses_raise_repair_once_when_assistant_disappears(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """Доступный ассистент пропал: первый POST поднимает repair один раз."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    entry = await async_add_entry(hass, conversation.HOME_ASSISTANT_AGENT)
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None

    client = await hass_client()
    body = responses_body(entry.entry_id, "включи свет", noise=False)
    started = await client.post("/api/quasar_assist/v1/responses", json=body)
    assert started.status == 200
    assert len(mock.calls) == 1

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "custom_components.quasar_assist.runtime.agent_is_available",
        unavailable,
    )
    caplog.clear()
    first = await client.post("/api/quasar_assist/v1/responses", json=body)
    assert first.status == 200
    assert_completed(await first.json(), PHRASE_FAILED)
    assert len(mock.calls) == 1
    assert len(_own(caplog, "недоступен")) == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None

    second = await client.post("/api/quasar_assist/v1/responses", json=body)
    assert second.status == 200
    assert_completed(await second.json(), PHRASE_FAILED)
    assert len(mock.calls) == 1
    assert len(_own(caplog, "недоступен")) == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
