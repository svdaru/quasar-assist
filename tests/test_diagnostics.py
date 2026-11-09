"""Диагностика без токена и без текста реплик."""

import json
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.quasar_assist.const import CONF_CONVERSATION_TTL
from custom_components.quasar_assist.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.common import (
    PipelineCapture,
    async_add_entry,
    async_create_pipeline,
    patch_execute,
    responses_body,
)


async def test_diagnostics_hide_token_and_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: Any,
) -> None:
    """SEC-AC-02: скачанная диагностика не содержит токен и реплику."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id, title="Спальня")
    client = await hass_client()
    phrase = "секретная реплика диагностики"
    response = await client.post(
        "/api/quasar_assist/v1/responses",
        json=responses_body(entry.entry_id, phrase, noise=False),
    )
    assert response.status == 200
    payload = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(payload, ensure_ascii=False)
    assert phrase not in dumped
    assert hass_access_token not in dumped
    assert "authorization" not in dumped.lower()
    assert "Bearer" not in dumped
    assert payload["dialogue_open"] is True
    assert payload["pipeline_unavailable"] is False
    assert payload["entry_id"] == entry.entry_id
    assert payload["title"] == "Спальня"
    assert payload["pipeline_id"] == pipeline_id
    assert payload[CONF_CONVERSATION_TTL] == entry.data[CONF_CONVERSATION_TTL]
    assert set(payload) == {
        "entry_id",
        "title",
        "pipeline_id",
        "conversation_ttl",
        "dialogue_open",
        "pipeline_unavailable",
    }


async def test_diagnostics_without_runtime(hass: HomeAssistant) -> None:
    """Незапущенная запись не показывает открытый диалог."""
    await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, "missing-pipeline", title="Пусто")
    payload = await async_get_config_entry_diagnostics(hass, entry)
    assert payload["dialogue_open"] is False
    assert payload["pipeline_unavailable"] is False
    assert payload["pipeline_id"] == "missing-pipeline"
    assert payload["title"] == "Пусто"
