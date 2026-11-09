"""Создание и перенастройка записи."""

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import SelectSelector
from homeassistant.util import dt as dt_util

from custom_components.quasar_assist.config_flow import (
    QuasarAssistConfigFlow,
    parse_ttl,
    pipeline_title,
)
from custom_components.quasar_assist.const import (
    ABORT_NO_PIPELINES,
    CONF_CONVERSATION_TTL,
    CONF_PIPELINE_ID,
    DEFAULT_CONVERSATION_TTL,
    DOMAIN,
    ERROR_INVALID_TTL,
    ERROR_PIPELINE_NOT_FOUND,
)
from custom_components.quasar_assist.pipeline import pipeline_is_available
from tests.common import (
    Clock,
    PipelineCapture,
    assert_completed,
    async_create_pipeline,
    async_prepare_assist,
    patch_clock,
    patch_execute,
    patch_session_ids,
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


def test_pipeline_title_uses_name_or_id() -> None:
    """Пустое имя конвейера не оставляет запись без названия."""
    assert pipeline_title("pipe", {"pipe": "Спальня"}) == "Спальня"
    assert pipeline_title("pipe", {"pipe": ""}) == "pipe"
    assert pipeline_title("pipe", {}) == "pipe"


def _select_config(schema: object) -> dict[str, Any]:
    raw = getattr(schema, "schema", None)
    assert isinstance(raw, dict)
    for value in raw.values():
        config = getattr(value, "config", None)
        if isinstance(config, dict) and "options" in config:
            assert isinstance(value, SelectSelector)
            return config
    raise AssertionError("в форме нет выбора конвейера")


async def test_abort_without_pipelines(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без конвейера запись не создаётся."""
    await async_prepare_assist(hass)
    monkeypatch.setattr(
        "custom_components.quasar_assist.config_flow.list_pipelines",
        lambda _hass: {},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == ABORT_NO_PIPELINES
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_create_entry_lists_every_pipeline_without_free_text(
    hass: HomeAssistant,
) -> None:
    """AC-06: выбор из списка по имени, срок 90, тот же конвейер можно снова."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    known = {pipeline.id: pipeline.name for pipeline in async_get_pipelines(hass)}
    assert pipeline_id in known

    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert first["type"] == "form"
    select = _select_config(first["data_schema"])
    assert select["custom_value"] is False
    assert select["sort"] is False
    options = select["options"]
    assert {item["value"] for item in options} == set(known)
    assert {item["value"]: item["label"] for item in options} == known

    created = await hass.config_entries.flow.async_configure(
        first["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id},
    )
    assert created["type"] == "create_entry"
    assert created["data"] == {
        CONF_PIPELINE_ID: pipeline_id,
        CONF_CONVERSATION_TTL: DEFAULT_CONVERSATION_TTL,
    }
    assert created["result"].unique_id is None
    assert created["title"] == "Спальня"

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    again = await hass.config_entries.flow.async_configure(
        second["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 30},
    )
    assert again["type"] == "create_entry"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert {entry.data[CONF_PIPELINE_ID] for entry in entries} == {pipeline_id}
    assert {entry.entry_id for entry in entries} == {
        created["result"].entry_id,
        again["result"].entry_id,
    }
    assert entries[0].runtime_data.conversation_id is None


async def test_rejects_unknown_pipeline(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Конвейер проверяется до создания записи."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    monkeypatch.setattr(
        "custom_components.quasar_assist.config_flow.pipeline_is_available",
        lambda *_args, **_kwargs: False,
    )
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 90},
    )
    assert result["type"] == "form"
    errors = result["errors"]
    assert errors is not None
    assert errors[CONF_PIPELINE_ID] == ERROR_PIPELINE_NOT_FOUND
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert pipeline_is_available(hass, "missing-pipeline") is False


async def test_rejects_invalid_ttl(hass: HomeAssistant) -> None:
    """Некорректный срок не создаёт запись."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 1.5},
    )
    assert result["type"] == "form"
    errors = result["errors"]
    assert errors is not None
    assert errors[CONF_CONVERSATION_TTL] == ERROR_INVALID_TTL
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_check_reports_both_errors(hass: HomeAssistant) -> None:
    """Чужой идентификатор и чужой срок возвращаются вместе."""
    await async_prepare_assist(hass)
    flow = QuasarAssistConfigFlow()
    flow.hass = hass
    errors = flow._check(
        {CONF_PIPELINE_ID: 5, CONF_CONVERSATION_TTL: "скоро"},
        {"known": "Спальня"},
    )
    assert errors == {
        CONF_PIPELINE_ID: ERROR_PIPELINE_NOT_FOUND,
        CONF_CONVERSATION_TTL: ERROR_INVALID_TTL,
    }


async def test_reconfigure_keeps_entry_and_starts_new_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-09: смена конвейера и срока не удаляет запись и ограничивает таймер."""
    bedroom_id, engine = await async_create_pipeline(hass, "Спальня")
    kitchen_id, _engine = await async_create_pipeline(hass, "Кухня", engine)
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    clock = Clock(dt_util.utcnow())
    patch_clock(monkeypatch, clock)

    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    created = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: bedroom_id, CONF_CONVERSATION_TTL: 90},
    )
    entry = created["result"]
    client = await hass_client()
    first = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert first.status == 200
    assert_completed(await first.json(), "Включила свет")
    assert capture.inputs[0].run.pipeline.id == bedroom_id
    assert sessions[0] is None
    open_id = entry.runtime_data.conversation_id
    assert open_id is not None

    reconfigure = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert reconfigure["type"] == "form"
    updated = await hass.config_entries.flow.async_configure(
        reconfigure["flow_id"],
        {CONF_PIPELINE_ID: kitchen_id, CONF_CONVERSATION_TTL: 15},
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert set(entry.data) == {CONF_PIPELINE_ID, CONF_CONVERSATION_TTL}
    assert entry.data[CONF_PIPELINE_ID] == kitchen_id
    assert entry.data[CONF_CONVERSATION_TTL] == 15
    assert entry.title == "Кухня"
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    assert entry.runtime_data.conversation_id is None

    second = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "включи чайник", noise=False),
    )
    assert second.status == 200
    assert capture.inputs[1].run.pipeline.id == kitchen_id
    assert sessions[-1] is None
    assert entry.runtime_data.conversation_id != open_id

    clock.moment = clock.moment + timedelta(seconds=15)
    await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, "ещё", noise=False),
    )
    assert capture.inputs[-1].intent_input == "ещё"
    assert sessions[-1] is None


async def test_reconfigure_rejects_invalid_input(
    hass: HomeAssistant,
) -> None:
    """Неверный срок на перенастройке оставляет прежние данные."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    created = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 90},
    )
    entry = created["result"]
    reconfigure = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        reconfigure["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id, CONF_CONVERSATION_TTL: 1.5},
    )
    assert result["type"] == "form"
    errors = result["errors"]
    assert errors is not None
    assert errors[CONF_CONVERSATION_TTL] == ERROR_INVALID_TTL
    assert entry.data[CONF_CONVERSATION_TTL] == 90


async def test_reconfigure_aborts_without_pipelines(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Перенастройка без конвейеров не удаляет запись."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    created = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {CONF_PIPELINE_ID: pipeline_id},
    )
    entry = created["result"]
    monkeypatch.setattr(
        "custom_components.quasar_assist.config_flow.list_pipelines",
        lambda _hass: {},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["type"] == "abort"
    assert result["reason"] == ABORT_NO_PIPELINES
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    assert entry.data[CONF_PIPELINE_ID] == pipeline_id
