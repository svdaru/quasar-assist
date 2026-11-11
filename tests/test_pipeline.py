"""Список конвейеров и края запуска."""

import asyncio
import logging
from typing import Any

import pytest
from homeassistant.components.assist_pipeline.pipeline import PipelineInput
from homeassistant.core import Context, HomeAssistant

from custom_components.quasar_assist.const import PHRASE_FAILED, RESPONSES_URL
from custom_components.quasar_assist.pipeline import (
    async_run_pipeline,
    list_pipelines,
    pipeline_is_available,
)
from tests.common import (
    assert_completed,
    async_add_entry,
    async_create_pipeline,
    responses_body,
)


async def test_list_before_component_is_loaded(hass: HomeAssistant) -> None:
    """Пока конвейер не загружен, списка нет и идентификатор недоступен."""
    assert list_pipelines(hass) == {}
    assert pipeline_is_available(hass, "") is False
    assert pipeline_is_available(hass, "missing-pipeline") is False
    assert (
        await async_run_pipeline(hass, "включи свет", None, Context(), "missing")
        is None
    )


async def test_list_includes_created_pipeline(hass: HomeAssistant) -> None:
    """Список не фильтрует созданный конвейер."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Кухня")
    listed = list_pipelines(hass)
    assert pipeline_id in listed
    assert listed[pipeline_id] == "Кухня"
    assert pipeline_is_available(hass, pipeline_id) is True
    assert pipeline_is_available(hass, "missing-pipeline") is False


async def test_run_missing_pipeline_does_not_execute(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неизвестный идентификатор не доходит до PipelineInput.execute."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("запуск не должен начаться")

    monkeypatch.setattr(PipelineInput, "execute", forbidden)
    assert (
        await async_run_pipeline(hass, "включи свет", None, Context(), "missing")
        is None
    )
    assert pipeline_is_available(hass, pipeline_id) is True


async def test_run_reraises_cancellation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отмена задачи не превращается в фразу о неудаче."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")

    async def cancelled(self: PipelineInput, validate: bool = False) -> None:
        del self, validate
        raise asyncio.CancelledError

    monkeypatch.setattr(PipelineInput, "execute", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await async_run_pipeline(hass, "включи свет", None, Context(), pipeline_id)


async def test_run_logs_exception_type_without_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой запуска: тип и реплика в одной строке хода, без текста исключения."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)
    phrase = "включи свет"
    secret = "текст-исключения-запуска"

    async def broken(self: PipelineInput, validate: bool = False) -> None:
        del self, validate
        raise RuntimeError(secret)

    monkeypatch.setattr(PipelineInput, "execute", broken)
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    reply = await async_run_pipeline(hass, phrase, None, Context(), pipeline_id)
    assert reply is not None
    assert reply.error_type == "RuntimeError"
    assert reply.speech == ""
    assert not [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    ]

    caplog.clear()
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, phrase, noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    own_records = [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    ]
    debug = [
        record.getMessage() for record in own_records if record.levelno == logging.DEBUG
    ]
    higher = "\n".join(
        record.getMessage() for record in own_records if record.levelno > logging.DEBUG
    )
    own = "\n".join(record.getMessage() for record in own_records)
    assert len(debug) == 1
    assert "RuntimeError" in debug[0]
    assert phrase in debug[0]
    assert secret not in own
    assert phrase not in higher


async def test_run_reraises_keyboard_interrupt(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Останов интерпретатора не становится фразой о неудаче."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")

    async def stopped(self: Any, validate: bool = False) -> None:
        del self, validate
        raise KeyboardInterrupt

    monkeypatch.setattr(PipelineInput, "execute", stopped)
    with pytest.raises(KeyboardInterrupt):
        await async_run_pipeline(hass, "включи свет", None, Context(), pipeline_id)
