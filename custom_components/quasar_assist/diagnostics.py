"""Скачиваемая диагностика без токена и без текста реплик."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AGENT_ID, CONF_CONVERSATION_TTL
from .runtime import QuasarAssistRuntimeData


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Собрать диагностику записи.

    В ответе нет токена, заголовка авторизации и текста реплик.
    """
    runtime = getattr(entry, "runtime_data", None)
    dialogue_open = False
    assistant_unavailable = False
    if isinstance(runtime, QuasarAssistRuntimeData):
        dialogue_open = runtime.conversation_id is not None
        assistant_unavailable = runtime.assistant_unavailable
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "agent_id": entry.data.get(CONF_AGENT_ID),
        "conversation_ttl": entry.data.get(CONF_CONVERSATION_TTL),
        "dialogue_open": dialogue_open,
        "assistant_unavailable": assistant_unavailable,
    }
