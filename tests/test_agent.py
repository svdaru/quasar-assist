"""Список ассистентов."""

from typing import Any

from homeassistant.components import conversation
from homeassistant.components.conversation.const import (
    DATA_COMPONENT,
    HOME_ASSISTANT_AGENT,
)
from homeassistant.core import HomeAssistant

from custom_components.quasar_assist.agent import agent_is_available, async_list_agents
from tests.common import async_register_agent


async def test_list_without_conversation(hass: HomeAssistant) -> None:
    """Пока диалог не загружен, списка нет и сущность не ищется."""
    assert async_list_agents(hass) == {}
    assert agent_is_available(hass, HOME_ASSISTANT_AGENT) is False
    assert agent_is_available(hass, "") is False
    assert agent_is_available(hass, "conversation.other") is False
    assert DATA_COMPONENT not in hass.data


async def test_list_includes_default_and_registered(
    hass: HomeAssistant,
    monkeypatch: Any,
) -> None:
    """Список не фильтрует встроенного ассистента и зарегистрированного."""
    other = await async_register_agent(hass, "Кухня")
    agents = async_list_agents(hass)
    assert HOME_ASSISTANT_AGENT in agents
    assert other in agents
    assert agent_is_available(hass, HOME_ASSISTANT_AGENT) is True
    assert agent_is_available(hass, other) is True

    component = hass.data[DATA_COMPONENT]
    entities = list(component.entities)
    original = conversation.async_get_agent_info
    hidden = entities[0].entity_id if entities else None

    def info(hass: HomeAssistant, agent_id: str | None = None) -> object:
        if hidden is not None and agent_id == hidden:
            return None
        return original(hass, agent_id)

    monkeypatch.setattr(conversation, "async_get_agent_info", info)
    listed = async_list_agents(hass)
    if hidden is not None:
        assert hidden not in listed
    assert other in listed


async def test_key_error_hides_builtin_agent(
    hass: HomeAssistant,
    monkeypatch: Any,
) -> None:
    """KeyError при чтении ассистента: недоступен, встроенный в список не попадает."""

    def missing(*_args: object, **_kwargs: object) -> None:
        raise KeyError

    # async_get_agent связан в agent.py: патчить нужно это имя.
    monkeypatch.setattr(
        "custom_components.quasar_assist.agent.async_get_agent",
        missing,
    )
    monkeypatch.setattr(conversation, "async_get_agent_info", missing)

    assert agent_is_available(hass, HOME_ASSISTANT_AGENT) is False
    assert HOME_ASSISTANT_AGENT not in async_list_agents(hass)
