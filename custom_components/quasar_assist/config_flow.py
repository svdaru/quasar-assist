"""Настройка записи: голосовой ассистент и срок диалога."""

from typing import Any, cast

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .agent import agent_is_available, async_list_agents
from .const import (
    ABORT_NO_ASSISTANTS,
    CONF_AGENT_ID,
    CONF_CONVERSATION_TTL,
    DEFAULT_CONVERSATION_TTL,
    DOMAIN,
    ERROR_ASSISTANT_NOT_FOUND,
    ERROR_INVALID_TTL,
)


def parse_ttl(value: object) -> int | None:
    """Преобразовать срок в целые секунды от 1. Иначе None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    ttl = int(value)
    if ttl < 1:
        return None
    return ttl


def _schema(agents: dict[str, str]) -> probatio.Schema:
    """Схема шага: выбор из списка, без свободного ввода agent_id."""
    options: list[SelectOptionDict] = [
        {"value": agent_id, "label": name} for agent_id, name in agents.items()
    ]
    return probatio.Schema(
        {
            probatio.Required(CONF_AGENT_ID): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            ),
            probatio.Required(
                CONF_CONVERSATION_TTL,
                default=DEFAULT_CONVERSATION_TTL,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
    )


class QuasarAssistConfigFlow(ConfigFlow, domain=DOMAIN):
    """Создание и перенастройка записи без своего идентификатора."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Создать запись, только если ассистент выбран из настроенных."""
        agents = async_list_agents(self.hass)
        if not agents:
            return self.async_abort(reason=ABORT_NO_ASSISTANTS)

        errors: dict[str, str] = {}
        if user_input is not None:
            checked = self._check(user_input, agents)
            if isinstance(checked, dict):
                errors = checked
            else:
                agent_id, ttl = checked
                return self.async_create_entry(
                    title=agents.get(agent_id) or agent_id,
                    data={
                        CONF_AGENT_ID: agent_id,
                        CONF_CONVERSATION_TTL: ttl,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(agents),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Сменить ассистента или срок, не удаляя запись."""
        entry = self._get_reconfigure_entry()
        agents = async_list_agents(self.hass)
        if not agents:
            return self.async_abort(reason=ABORT_NO_ASSISTANTS)

        errors: dict[str, str] = {}
        if user_input is not None:
            checked = self._check(user_input, agents)
            if isinstance(checked, dict):
                errors = checked
            else:
                agent_id, ttl = checked
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_AGENT_ID: agent_id,
                        CONF_CONVERSATION_TTL: ttl,
                    },
                )

        suggested = {
            CONF_AGENT_ID: entry.data.get(CONF_AGENT_ID),
            CONF_CONVERSATION_TTL: entry.data.get(
                CONF_CONVERSATION_TTL,
                DEFAULT_CONVERSATION_TTL,
            ),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_schema(agents), suggested),
            errors=errors,
        )

    @callback
    def _check(
        self,
        user_input: dict[str, Any],
        agents: dict[str, str],
    ) -> dict[str, str] | tuple[str, int]:
        """Проверить ассистента и срок до создания или обновления записи."""
        raw_agent = user_input.get(CONF_AGENT_ID)
        agent_id = raw_agent if isinstance(raw_agent, str) else ""
        errors: dict[str, str] = {}
        if agent_id not in agents or not agent_is_available(self.hass, agent_id):
            errors[CONF_AGENT_ID] = ERROR_ASSISTANT_NOT_FOUND
        ttl = parse_ttl(user_input.get(CONF_CONVERSATION_TTL))
        if ttl is None:
            errors[CONF_CONVERSATION_TTL] = ERROR_INVALID_TTL
        if errors:
            return errors
        return agent_id, cast(int, ttl)
