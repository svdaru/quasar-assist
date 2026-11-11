"""Маршрут фразы методами встроенного агента.

Совпадение не запускает автоматизацию, сценарий или намерение.
Язык сравнения — язык Home Assistant.
"""

from homeassistant.components.conversation.agent_manager import get_agent_manager
from homeassistant.components.conversation.const import HOME_ASSISTANT_AGENT
from homeassistant.components.conversation.models import ConversationInput
from homeassistant.core import Context, HomeAssistant

ROUTE_LLM = "llm"
ROUTE_YANDEX = "yandex"


class _MissingBuiltinAgentError(Exception):
    """Встроенного агента нет.

    Сообщение пустое: журнал хода пишет только имя типа. Вызывающий
    отвечает пустым HTTP 500 и не запускает конвейер.
    """


async def match_route(hass: HomeAssistant, text: str) -> str:
    """Вернуть llm или yandex по методам встроенного агента.

    hass — Home Assistant. Агент — default_agent менеджера разговора:
    встроенный conversation.home_assistant. Агент выбранного конвейера
    эти методы не подменяет и не вызывается. text — последняя реплика
    user без изменений.

    Сначала async_recognize_sentence_trigger. Совпадение автоматизации,
    сценария или уже зарегистрированной копии триггера даёт llm, и
    async_recognize_intent не вызывается. Иначе async_recognize_intent
    со strict_intents_only. Результат без несопоставленных сущностей
    даёт llm. Нет результата или есть несопоставленные сущности — yandex.

    Язык входа — hass.config.language. В методы не передаются device_id
    и satellite_id. Исполнение намерения, автоматизации и сценария
    не запускается.

    Нет встроенного агента — _MissingBuiltinAgentError без текста.
    Прочее исключение одного из двух методов уходит вызывающему.
    CancelledError, KeyboardInterrupt, SystemExit и GeneratorExit
    этим ответом не становятся.
    """
    agent = get_agent_manager(hass).default_agent
    if agent is None:
        raise _MissingBuiltinAgentError
    user_input = ConversationInput(
        text=text,
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language=hass.config.language,
        agent_id=HOME_ASSISTANT_AGENT,
    )
    if await agent.async_recognize_sentence_trigger(user_input) is not None:
        return ROUTE_LLM
    result = await agent.async_recognize_intent(
        user_input,
        strict_intents_only=True,
    )
    if result is not None and not result.unmatched_entities:
        return ROUTE_LLM
    return ROUTE_YANDEX
