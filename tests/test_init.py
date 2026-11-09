"""Загрузка записи, журнал доступности и repair."""

import logging
from typing import Any, cast

import pytest
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quasar_assist.const import (
    CONF_CONVERSATION_TTL,
    CONF_PIPELINE_ID,
    DOMAIN,
    PHRASE_FAILED,
)
from custom_components.quasar_assist.http import runtime_of
from custom_components.quasar_assist.runtime import (
    QuasarAssistRuntimeData,
    conversation_ttl,
    issue_id,
    legacy_issue_id,
    pipeline_id_of,
    refresh_pipeline,
)
from tests.common import (
    PipelineCapture,
    assert_completed,
    assert_same_entry,
    async_add_entry,
    async_create_pipeline,
    async_prepare_assist,
    patch_execute,
    responses_body,
)


def test_runtime_rejects_foreign_values(hass: HomeAssistant) -> None:
    """Чужой срок и пустое состояние не открывают диалог.

    Старая запись хранит прежний ключ. Пакет читает только идентификатор
    конвейера, поэтому цель из того ключа не берётся.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"agent_id": "conversation.home_assistant", CONF_CONVERSATION_TTL: True},
    )
    entry.add_to_hass(hass)
    assert pipeline_id_of(entry) == ""
    assert "agent_id" in entry.data
    assert conversation_ttl(entry) == 90
    entry.runtime_data = object()
    assert refresh_pipeline(hass, entry) is False
    assert runtime_of(entry) is None


def _own(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
        and needle in record.getMessage()
    ]


def _setup_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "homeassistant.config_entries"
        and record.levelno >= logging.ERROR
        and "Error setting up entry" in record.getMessage()
    ]


async def test_live_pipeline_loads(hass: HomeAssistant) -> None:
    """Живой конвейер загружает запись, repair не поднимается."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, QuasarAssistRuntimeData)
    assert entry.runtime_data.pipeline_unavailable is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None


async def test_startup_logs_missing_pipeline_once(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Пропавший на старте конвейер пишется один раз исключением и поднимает repair."""
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    await async_prepare_assist(hass)
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    caplog.set_level("ERROR", logger="homeassistant.config_entries")
    entry = await async_add_entry(hass, "missing-pipeline")
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.SETUP_ERROR
    assert_same_entry(hass.config_entries.async_get_entry(entry.entry_id), entry)
    assert runtime_of(entry) is None
    assert _own(caplog, "недоступен") == []
    assert len(_setup_errors(caplog)) == 1
    assert "no longer available" in _setup_errors(caplog)[0]
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id)) is not None
    )
    assert capture.inputs == []

    client = await hass_client()
    response = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    assert capture.inputs == []
    assert runtime_of(entry) is None
    assert _own(caplog, "недоступен") == []
    assert len(_setup_errors(caplog)) == 1


async def test_legacy_entry_does_not_start_until_pipeline_is_chosen(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N09: запись без конвейера не стартует и не читает старый ключ как цель."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    seen: list[object] = []
    original = async_get_pipeline

    def wrapped(hass: HomeAssistant, pipeline_id: str | None = None) -> object:
        seen.append(pipeline_id)
        return original(hass, pipeline_id)

    monkeypatch.setattr(
        "custom_components.quasar_assist.pipeline.async_get_pipeline",
        wrapped,
    )
    legacy_value = "conversation.home_assistant"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Старая",
        data={"agent_id": legacy_value, CONF_CONVERSATION_TTL: 40},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert runtime_of(entry) is None
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    assert "agent_id" in entry.data
    assert legacy_value not in seen
    assert capture.inputs == []

    client = await hass_client()
    response = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    assert capture.inputs == []

    started = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    updated = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 40},
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert set(entry.data) == {CONF_PIPELINE_ID, CONF_CONVERSATION_TTL}
    assert "agent_id" not in entry.data
    assert entry.data[CONF_PIPELINE_ID] == pipeline_id
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None


async def test_reload_clears_repair_left_from_previous_runtime(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Новая загрузка с чистым runtime снимает уже существующий repair."""
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, "missing-pipeline")
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    hass.config_entries.async_update_entry(
        entry,
        data={CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 90},
    )
    caplog.clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.pipeline_unavailable is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None
    assert len(_own(caplog, "снова доступен")) == 1
    assert refresh_pipeline(hass, entry) is True
    assert len(_own(caplog, "снова доступен")) == 1


async def test_reconfigure_to_live_pipeline_clears_repair(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Перенастройка на живой конвейер снимает repair."""
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, "missing-pipeline")
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    started = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    updated = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 90},
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None
    assert len(_own(caplog, "снова доступен")) == 1


async def test_unload_keeps_repair_and_remove_clears_it(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Выгрузка repair не снимает, удаление записи снимает."""
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    await async_prepare_assist(hass)
    entry = await async_add_entry(hass, "missing-pipeline")
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


async def test_responses_raise_repair_once_when_pipeline_disappears(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Доступный конвейер пропал: первый POST поднимает repair один раз."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    caplog.set_level("INFO", logger="custom_components.quasar_assist")
    entry = await async_add_entry(hass, pipeline_id)
    repair = issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is None

    client = await hass_client()
    body = responses_body(entry.entry_id, "включи свет", noise=False)
    started = await client.post("/api/quasar_assist/v1/responses", json=body)
    assert started.status == 200
    assert len(capture.inputs) == 1

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "custom_components.quasar_assist.runtime.pipeline_is_available",
        unavailable,
    )
    caplog.clear()
    first = await client.post("/api/quasar_assist/v1/responses", json=body)
    assert first.status == 200
    assert_completed(await first.json(), PHRASE_FAILED)
    assert len(capture.inputs) == 1
    assert len(_own(caplog, "недоступен")) == 1
    assert entry.runtime_data.conversation_id is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None

    second = await client.post("/api/quasar_assist/v1/responses", json=body)
    assert second.status == 200
    assert_completed(await second.json(), PHRASE_FAILED)
    assert len(capture.inputs) == 1
    assert len(_own(caplog, "недоступен")) == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None


def _plant_legacy_repair(hass: HomeAssistant, entry_id: str) -> str:
    """Положить в реестр repair версии 0.1.0."""
    repair = f"assistant_unavailable_{entry_id}"
    assert repair == legacy_issue_id(entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        repair,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="assistant_unavailable",
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    return repair


async def test_legacy_assistant_repair_cleared_on_setup_reconfigure_and_remove(
    hass: HomeAssistant,
) -> None:
    """Старт записи 0.1.0, перенастройка и удаление снимают прежний repair.

    Новый repair пропавшего конвейера поднимается и снимается своим
    идентификатором.
    """
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Старая",
        data={"agent_id": "conversation.home_assistant"},
        entry_id="legacy-entry",
    )
    entry.add_to_hass(hass)
    legacy = _plant_legacy_repair(hass, entry.entry_id)
    current = issue_id(entry.entry_id)
    assert legacy != current

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, legacy) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, current) is not None

    _plant_legacy_repair(hass, entry.entry_id)
    started = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    updated = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 90},
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.LOADED
    assert "agent_id" not in entry.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, legacy) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, current) is None

    _plant_legacy_repair(hass, entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, legacy) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, current) is None


async def test_legacy_assistant_repair_cleared_for_disabled_entry(
    hass: HomeAssistant,
) -> None:
    """Прямой вызов загрузки снимает старый repair выключенной записи.

    Этот вызов не является стартом домена в Home Assistant: без
    включённой записи домен не загружается. async_setup_entry для
    выключенной записи не вызывается. Новый repair конвейера не создаётся.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Выключена",
        data={"agent_id": "conversation.home_assistant"},
        entry_id="disabled-entry",
        disabled_by=ConfigEntryDisabler.USER,
    )
    entry.add_to_hass(hass)
    legacy = _plant_legacy_repair(hass, entry.entry_id)
    current = issue_id(entry.entry_id)

    await async_prepare_assist(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert cast(ConfigEntryState, entry.state) is ConfigEntryState.NOT_LOADED
    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert ir.async_get(hass).async_get_issue(DOMAIN, legacy) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, current) is None


async def test_enabled_entry_clears_legacy_repair_of_disabled_neighbor(
    hass: HomeAssistant,
) -> None:
    """Включённая запись поднимает домен и снимает старый repair соседки.

    Цикл работает, когда компонент уже загружен. Выключенная соседка
    остаётся выключенной, новый repair конвейера для неё не создаётся.
    """
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    disabled = MockConfigEntry(
        domain=DOMAIN,
        title="Выключена",
        data={"agent_id": "conversation.home_assistant"},
        entry_id="disabled-neighbor",
        disabled_by=ConfigEntryDisabler.USER,
    )
    enabled = MockConfigEntry(
        domain=DOMAIN,
        title="Включена",
        data={CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 90},
        entry_id="enabled-neighbor",
    )
    disabled.add_to_hass(hass)
    enabled.add_to_hass(hass)
    legacy = _plant_legacy_repair(hass, disabled.entry_id)
    current = issue_id(disabled.entry_id)
    assert legacy == f"assistant_unavailable_{disabled.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, legacy) is not None
    assert DOMAIN not in hass.config.components

    assert await hass.config_entries.async_setup(enabled.entry_id)
    await hass.async_block_till_done()

    assert DOMAIN in hass.config.components
    assert cast(ConfigEntryState, enabled.state) is ConfigEntryState.LOADED
    assert cast(ConfigEntryState, disabled.state) is ConfigEntryState.NOT_LOADED
    assert disabled.disabled_by is ConfigEntryDisabler.USER
    assert ir.async_get(hass).async_get_issue(DOMAIN, legacy) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, current) is None
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(enabled.entry_id)) is None
    )
