"""Скачиваемая диагностика без токена и без текста реплик."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CONVERSATION_TTL, CONF_PIPELINE_ID
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
    pipeline_unavailable = False
    if isinstance(runtime, QuasarAssistRuntimeData):
        dialogue_open = runtime.conversation_id is not None
        pipeline_unavailable = runtime.pipeline_unavailable
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "pipeline_id": entry.data.get(CONF_PIPELINE_ID),
        "conversation_ttl": entry.data.get(CONF_CONVERSATION_TTL),
        "dialogue_open": dialogue_open,
        "pipeline_unavailable": pipeline_unavailable,
    }
