"""Текстовый конвейер Assist для запросов Quasar Bridge."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, ISSUE_PIPELINE_UNAVAILABLE
from .http import ModelsView, ResponsesView
from .pipeline import pipeline_is_available
from .runtime import (
    QuasarAssistRuntimeData,
    create_unavailable_issue,
    delete_legacy_issue,
    issue_id,
    pipeline_id_of,
    refresh_pipeline,
    release_chat_session,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type QuasarAssistConfigEntry = ConfigEntry[QuasarAssistRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Зарегистрировать адреса. Записи при этом могут отсутствовать.

    Цикл работает, когда компонент уже загружен, и снимает старый
    repair версии 0.1.0 и у выключенной соседки. Выключенную запись
    Home Assistant в async_setup_entry не передаёт. Новый repair
    пропавшего конвейера здесь не создаётся.
    """
    hass.http.register_view(ResponsesView())
    hass.http.register_view(ModelsView())
    for entry in hass.config_entries.async_entries(DOMAIN):
        delete_legacy_issue(hass, entry.entry_id)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: QuasarAssistConfigEntry
) -> bool:
    """Загрузить запись, если выбранный конвейер ещё есть.

    Пропавший конвейер сам не возвращается. Старая запись без идентификатора
    конвейера тоже не стартует: человек выбирает конвейер заново.
    Старт поднимает repair и ConfigEntryError, запись остаётся в реестре.
    Журнал этой потери пишет само исключение, второе предупреждение
    интеграция не добавляет. Старый repair версии 0.1.0 снимается
    и при неудачном старте.
    """
    delete_legacy_issue(hass, entry.entry_id)
    if not pipeline_is_available(hass, pipeline_id_of(entry)):
        create_unavailable_issue(hass, entry)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key=ISSUE_PIPELINE_UNAVAILABLE,
            translation_placeholders={"entry_id": entry.entry_id},
        )

    entry.runtime_data = QuasarAssistRuntimeData()
    refresh_pipeline(hass, entry)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: QuasarAssistConfigEntry
) -> bool:
    """Выгрузить запись. Общие адреса остаются, пока загружен компонент.

    Удержание сессии Assist прекращается вместе с выгрузкой. Repair
    при этом не снимается.
    """
    release_chat_session(entry)
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: QuasarAssistConfigEntry
) -> None:
    """Убрать repair вместе с записью, включая идентификатор версии 0.1.0."""
    release_chat_session(entry)
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
    delete_legacy_issue(hass, entry.entry_id)
