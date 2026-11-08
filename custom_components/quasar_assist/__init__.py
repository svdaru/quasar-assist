"""Текстовый Assist для запросов Quasar Bridge."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .agent import agent_is_available
from .const import DOMAIN, ISSUE_ASSISTANT_UNAVAILABLE
from .http import ModelsView, ResponsesView
from .runtime import (
    QuasarAssistRuntimeData,
    agent_id_of,
    create_unavailable_issue,
    issue_id,
    refresh_assistant,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type QuasarAssistConfigEntry = ConfigEntry[QuasarAssistRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Зарегистрировать адреса. Записи при этом могут отсутствовать."""
    hass.http.register_view(ResponsesView())
    hass.http.register_view(ModelsView())
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: QuasarAssistConfigEntry
) -> bool:
    """Загрузить запись, если выбранный ассистент ещё есть.

    Пропавший ассистент сам не возвращается. Старт поднимает repair и
    ConfigEntryError, запись остаётся в реестре. Журнал этой потери пишет
    само исключение, второе предупреждение интеграция не добавляет.
    """
    if not agent_is_available(hass, agent_id_of(entry)):
        create_unavailable_issue(hass, entry)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key=ISSUE_ASSISTANT_UNAVAILABLE,
            translation_placeholders={"entry_id": entry.entry_id},
        )

    entry.runtime_data = QuasarAssistRuntimeData()
    refresh_assistant(hass, entry)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: QuasarAssistConfigEntry
) -> bool:
    """Выгрузить запись. Общие адреса остаются, пока загружен компонент."""
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: QuasarAssistConfigEntry
) -> None:
    """Убрать repair вместе с записью."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
