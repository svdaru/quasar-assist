"""Список голосовых ассистентов Home Assistant."""

from homeassistant.components import conversation
from homeassistant.components.conversation.agent_manager import (
    async_get_agent,
    get_agent_manager,
)
from homeassistant.components.conversation.const import (
    DATA_COMPONENT,
    HOME_ASSISTANT_AGENT,
)
from homeassistant.core import HomeAssistant


def agent_is_available(hass: HomeAssistant, agent_id: str) -> bool:
    """Проверить, что выбранный ассистент ещё зарегистрирован.

    Идентификатор с точкой ищет сущность. Пока компонент диалога не загружен,
    такого поиска нет: иначе чтение реестра сущностей падает.
    """
    if agent_id == "":
        return False
    if (
        "." in agent_id
        and agent_id != HOME_ASSISTANT_AGENT
        and DATA_COMPONENT not in hass.data
    ):
        return False
    try:
        return async_get_agent(hass, agent_id) is not None
    except KeyError:
        return False


def async_list_agents(hass: HomeAssistant) -> dict[str, str]:
    """Вернуть id и имя всех настроенных голосовых ассистентов.

    Список не фильтруется: встроенный ассистент, агенты записей и сущности диалога.
    """
    agents: dict[str, str] = {}
    try:
        default = conversation.async_get_agent_info(hass, HOME_ASSISTANT_AGENT)
    except KeyError:
        default = None
    if default is not None:
        agents[default.id] = default.name

    for info in get_agent_manager(hass).async_get_agent_info():
        agents[info.id] = info.name

    component = hass.data.get(DATA_COMPONENT)
    if component is None:
        return agents

    for entity in component.entities:
        entity_info = conversation.async_get_agent_info(hass, entity.entity_id)
        if entity_info is None:
            continue
        agents[entity_info.id] = entity_info.name
    return agents
