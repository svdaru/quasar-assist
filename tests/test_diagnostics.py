"""Диагностика без токена и без текста реплик."""

import json
from typing import Any

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant

from custom_components.quasar_assist.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.common import ConverseMock, async_add_entry, patch_converse, responses_body


async def test_diagnostics_hide_token_and_phrase(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: Any,
) -> None:
    """SEC-AC-02: скачанная диагностика не содержит токен и реплику."""
    mock = ConverseMock()
    patch_converse(monkeypatch, mock)
    entry = await async_add_entry(
        hass, conversation.HOME_ASSISTANT_AGENT, title="Спальня"
    )
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
    assert payload["assistant_unavailable"] is False
    assert payload["entry_id"] == entry.entry_id
    assert payload["title"] == "Спальня"
    assert set(payload) == {
        "entry_id",
        "title",
        "agent_id",
        "conversation_ttl",
        "dialogue_open",
        "assistant_unavailable",
    }
