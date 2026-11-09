"""Настройка записи: конвейер Assist и срок диалога."""

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

from .const import (
    ABORT_NO_PIPELINES,
    CONF_CONVERSATION_TTL,
    CONF_PIPELINE_ID,
    DEFAULT_CONVERSATION_TTL,
    DOMAIN,
    ERROR_INVALID_TTL,
    ERROR_PIPELINE_NOT_FOUND,
)
from .pipeline import list_pipelines, pipeline_is_available
from .runtime import delete_legacy_issue


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


def pipeline_title(pipeline_id: str, pipelines: dict[str, str]) -> str:
    """Название записи: имя конвейера, а если оно пустое — его идентификатор."""
    name = pipelines.get(pipeline_id, "")
    if name != "":
        return name
    return pipeline_id


def _schema(pipelines: dict[str, str]) -> probatio.Schema:
    """Схема шага: выбор из списка, без свободного ввода идентификатора."""
    options: list[SelectOptionDict] = [
        {"value": pipeline_id, "label": name} for pipeline_id, name in pipelines.items()
    ]
    return probatio.Schema(
        {
            probatio.Required(CONF_PIPELINE_ID): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                    sort=False,
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
        """Создать запись, только если конвейер выбран из настроенных."""
        pipelines = list_pipelines(self.hass)
        if not pipelines:
            return self.async_abort(reason=ABORT_NO_PIPELINES)

        errors: dict[str, str] = {}
        if user_input is not None:
            checked = self._check(user_input, pipelines)
            if isinstance(checked, dict):
                errors = checked
            else:
                pipeline_id, ttl = checked
                return self.async_create_entry(
                    title=pipeline_title(pipeline_id, pipelines),
                    data={
                        CONF_PIPELINE_ID: pipeline_id,
                        CONF_CONVERSATION_TTL: ttl,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(pipelines),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Сменить конвейер или срок, не удаляя запись.

        Данные заменяются целиком. После выбора в записи остаются только
        идентификатор конвейера и срок, даже если раньше там был другой ключ.
        """
        entry = self._get_reconfigure_entry()
        pipelines = list_pipelines(self.hass)
        if not pipelines:
            return self.async_abort(reason=ABORT_NO_PIPELINES)

        errors: dict[str, str] = {}
        if user_input is not None:
            checked = self._check(user_input, pipelines)
            if isinstance(checked, dict):
                errors = checked
            else:
                pipeline_id, ttl = checked
                delete_legacy_issue(self.hass, entry.entry_id)
                return self.async_update_reload_and_abort(
                    entry,
                    title=pipeline_title(pipeline_id, pipelines),
                    data={
                        CONF_PIPELINE_ID: pipeline_id,
                        CONF_CONVERSATION_TTL: ttl,
                    },
                )

        suggested: dict[str, Any] = {
            CONF_CONVERSATION_TTL: entry.data.get(
                CONF_CONVERSATION_TTL,
                DEFAULT_CONVERSATION_TTL,
            ),
        }
        current = entry.data.get(CONF_PIPELINE_ID)
        if isinstance(current, str) and current in pipelines:
            suggested[CONF_PIPELINE_ID] = current
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _schema(pipelines), suggested
            ),
            errors=errors,
        )

    @callback
    def _check(
        self,
        user_input: dict[str, Any],
        pipelines: dict[str, str],
    ) -> dict[str, str] | tuple[str, int]:
        """Проверить конвейер и срок до создания или обновления записи."""
        raw_pipeline = user_input.get(CONF_PIPELINE_ID)
        pipeline_id = raw_pipeline if isinstance(raw_pipeline, str) else ""
        errors: dict[str, str] = {}
        if pipeline_id not in pipelines or not pipeline_is_available(
            self.hass, pipeline_id
        ):
            errors[CONF_PIPELINE_ID] = ERROR_PIPELINE_NOT_FOUND
        ttl = parse_ttl(user_input.get(CONF_CONVERSATION_TTL))
        if ttl is None:
            errors[CONF_CONVERSATION_TTL] = ERROR_INVALID_TTL
        if errors:
            return errors
        return pipeline_id, cast(int, ttl)
