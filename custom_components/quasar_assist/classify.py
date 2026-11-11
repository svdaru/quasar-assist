"""Маршрут фразы по включённым триггерам conversation.

Совпадение не запускает автоматизацию, не регистрирует триггер и не
вызывает callback. Язык намерения — язык Home Assistant.
"""

from typing import Any

import probatio
from hassil.expression import Expression, Group, ListReference
from hassil.intents import Intents, WildcardSlotList
from hassil.recognize import recognize_all
from homeassistant.components.automation import DATA_COMPONENT
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.config_validation import boolean
from homeassistant.helpers.template import Template, is_template_string

ROUTE_LLM = "llm"
ROUTE_YANDEX = "yandex"

_TRIGGER_LIST_KEYS = ("trigger", "triggers")
_TRIGGER_WRAPPER = "triggers"
_CONVERSATION = "conversation"
_COMMAND = "command"
_ENABLED = "enabled"


def match_route(hass: HomeAssistant, text: str) -> str:
    """Вернуть llm, если фраза совпала бы с триггером, иначе yandex.

    text — последняя реплика user без изменений. Язык сравнения —
    hass.config.language, не язык конвейера и не язык из тела.
    Нет команд — yandex, это не ошибка. Исключение перечисления
    автоматизаций или разбора команды уходит вызывающему.
    """
    sentences = conversation_commands(hass)
    if not sentences:
        return ROUTE_YANDEX
    if phrase_matches(hass, text, sentences):
        return ROUTE_LLM
    return ROUTE_YANDEX


def conversation_commands(hass: HomeAssistant) -> list[str]:
    """Собрать команды включённых триггеров conversation.

    Берутся только автоматизации в состоянии on. Сценарий сюда не входит:
    читается компонент automation. Компонент не загружен — пустой список,
    не ошибка. Сырой конфиг сущности — поле raw_config. Ошибка рендера
    переменных триггера оставляет автоматизацию без команд.
    """
    component: Any = hass.data.get(DATA_COMPONENT)
    if component is None:
        return []
    commands: list[str] = []
    for entity in component.entities:
        if not hass.states.is_state(entity.entity_id, STATE_ON):
            continue
        raw_config = entity.raw_config
        if not isinstance(raw_config, dict):
            continue
        variables = _automation_variables(hass, entity)
        if variables is None:
            continue
        commands.extend(commands_in_config(raw_config, hass, variables))
    return commands


def commands_in_config(
    raw_config: dict[str, Any],
    hass: HomeAssistant | None = None,
    variables: dict[str, Any] | None = None,
) -> list[str]:
    """Достать команды из сырого конфига одной автоматизации.

    Список триггеров лежит в ключе trigger или triggers. Значение-словарь
    и список идут одним путём: ensure_list, затем один проход разворота.
    Словарь с единственным ключом triggers заменяется своим списком,
    повторная обёртка не разворачивается. Обычный триггер с platform
    или trigger остаётся одним элементом. Платформа conversation — это
    platform либо поле типа trigger. Команда — строка или список строк.
    Флаг enabled отсутствует — триггер включён.
    """
    template_variables = {"this": None} if variables is None else variables
    commands: list[str] = []
    for key in _TRIGGER_LIST_KEYS:
        block = raw_config.get(key)
        if not isinstance(block, dict | list):
            continue
        items = _flatten_trigger_list(_as_trigger_list(block))
        for item in items:
            if isinstance(item, dict):
                commands.extend(_commands_from_trigger(item, hass, template_variables))
    return commands


def phrase_matches(hass: HomeAssistant, text: str, sentences: list[str]) -> bool:
    """Совпадает ли фраза с командами так же, как триггер conversation.

    Фраза передаётся распознавателю без изменений. Ошибка разбора команды
    — исключение hassil, не тихое отсутствие совпадения.
    """
    intents = _trigger_intents(hass.config.language, sentences)
    for _result in recognize_all(text, intents):
        return True
    return False


def _trigger_intents(language: str, sentences: list[str]) -> Intents:
    """Собрать намерения так же, как пересборка триггера conversation.

    Словарь получает язык Home Assistant. Ссылки {slot} становятся
    wildcard-списками по slot_name. Встроенный диапазон {1..100:имя}
    остаётся числовым: распознаватель ищет его по list_name, и числовые
    слова выбирает поле language намерений.
    """
    intents = Intents.from_dict(
        {
            "language": language,
            "intents": {
                "conversation_commands": {"data": [{"sentences": sentences}]},
            },
        }
    )
    names: set[str] = set()
    for trigger_intent in intents.intents.values():
        for intent_data in trigger_intent.data:
            for sentence in intent_data.sentences:
                _collect_list_references(sentence.expression, names)
    for name in names:
        intents.slot_lists[name] = WildcardSlotList(name)
    return intents


def _commands_from_trigger(
    trigger: dict[str, Any],
    hass: HomeAssistant | None,
    variables: dict[str, Any],
) -> list[str]:
    """Команды одного триггера, если он включён и это conversation."""
    platform = trigger.get("platform")
    kind = trigger.get("trigger")
    if platform != _CONVERSATION and kind != _CONVERSATION:
        return []
    if not _trigger_is_enabled(trigger, hass, variables):
        return []
    return _command_strings(trigger.get(_COMMAND))


def _automation_variables(hass: HomeAssistant, entity: Any) -> dict[str, Any] | None:
    """Контекст шаблона enabled, как при подключении триггеров.

    Есть состояние — в this кладётся state.as_dict(), иначе None.
    Переменные триггера рендерятся в этот контекст с limited=True.
    TemplateError значит, что подключённых триггеров нет. Текст
    исключения не пишется.
    """
    state = hass.states.get(entity.entity_id)
    this = None if state is None else state.as_dict()
    variables: dict[str, Any] = {"this": this}
    trigger_variables = getattr(entity, "_trigger_variables", None)
    if not trigger_variables:
        return variables
    try:
        rendered: dict[str, Any] = trigger_variables.async_render(
            hass,
            variables,
            limited=True,
        )
    except TemplateError:
        return None
    return rendered


def _flatten_trigger_list(triggers: list[Any]) -> list[Any]:
    """Один проход, как _base_trigger_list_flatten.

    Словарь с единственным ключом triggers заменяется списком внутри.
    None внутри даёт пустой список, не-список — один элемент. Иной
    элемент остаётся собой. Обёртка внутри этого списка не разворачивается.
    """
    flat: list[Any] = []
    for item in triggers:
        if isinstance(item, dict) and len(item) == 1 and _TRIGGER_WRAPPER in item:
            flat.extend(_as_trigger_list(item[_TRIGGER_WRAPPER]))
        else:
            flat.append(item)
    return flat


def _as_trigger_list(value: object) -> list[Any]:
    """Список триггеров, как ensure_list в развороте обёртки."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _trigger_is_enabled(
    trigger: dict[str, Any],
    hass: HomeAssistant | None,
    variables: dict[str, Any],
) -> bool:
    """Включён ли триггер так же, как при подключении.

    Нет enabled — включён. False не входит, True входит. Строка сначала
    проходит boolean: истина входит, ложь нет. Отказ boolean — шаблон.
    Строка шаблона и готовый объект Template рендерятся с limited=True.
    Ложный результат и TemplateError триггер не включают, текст
    исключения не пишется. Строка, которую boolean отверг и которая не
    шаблон, — статический шаблон: непустой результат включает, пустой
    нет. Без hass строку шаблона посчитать нельзя, и триггер не входит.
    """
    if _ENABLED not in trigger:
        return True
    enabled: object = trigger[_ENABLED]
    if isinstance(enabled, str):
        try:
            return boolean(enabled)
        except probatio.Invalid:
            if hass is None and not is_template_string(enabled):
                return bool(enabled.strip())
            if hass is None:
                return False
            enabled = Template(enabled, hass)
    if isinstance(enabled, Template):
        try:
            enabled = enabled.async_render(
                variables,
                limited=True,
                log_fn=_ignore_template_log,
            )
        except TemplateError:
            return False
    return bool(enabled)


def _ignore_template_log(_level: int, _message: str) -> None:
    """Не писать текст шаблона: пропуск триггера не попадает в журнал."""
    return


def _command_strings(raw: object) -> list[str]:
    """Оставить строку или строки из списка. Иные значения — не команда."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def _collect_list_references(expression: Expression, list_names: set[str]) -> None:
    """Собрать имена списков из шаблона, как в пересборке триггера."""
    if isinstance(expression, Group):
        for item in expression.items:
            _collect_list_references(item, list_names)
    elif isinstance(expression, ListReference):
        list_names.add(expression.slot_name)
