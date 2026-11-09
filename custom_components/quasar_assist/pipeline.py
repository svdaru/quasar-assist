"""Запуск конвейера Assist и список конвейеров по имени."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.assist_pipeline.models import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_pipeline.pipeline import (
    PipelineInput,
    PipelineNotFound,
    PipelineRun,
    async_get_pipeline,
    async_get_pipelines,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.chat_session import async_get_chat_session

_LOGGER = logging.getLogger(__name__)

# Отмена задачи и останов интерпретатора не становятся фразой о неудаче.
_PASSTHROUGH_FAILURES = (
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
    asyncio.CancelledError,
)


@dataclass(frozen=True, slots=True)
class PipelineReply:
    """Речь события intent-end и идентификатор диалога этой речи."""

    speech: str
    conversation_id: str | None


def list_pipelines(hass: HomeAssistant) -> dict[str, str]:
    """Вернуть идентификатор и имя всех конвейеров Assist.

    Список не фильтруется. Пока компонент конвейера не загружен, он пуст.
    """
    try:
        pipelines = async_get_pipelines(hass)
    except KeyError:
        return {}
    return {pipeline.id: pipeline.name for pipeline in pipelines}


def pipeline_is_available(hass: HomeAssistant, pipeline_id: str) -> bool:
    """Проверить, что конвейер с этим идентификатором ещё есть.

    Пустой идентификатор недоступен. Вызов без идентификатора здесь не
    используется: иначе библиотека подставила бы предпочтительный конвейер.
    """
    if pipeline_id == "":
        return False
    try:
        async_get_pipeline(hass, pipeline_id)
    except KeyError, PipelineNotFound:
        return False
    return True


def _plain_speech(data: dict[str, Any]) -> tuple[str, str | None]:
    """Достать речь и идентификатор диалога из data события intent-end.

    Тип ответа не читается: непустая речь остаётся речью даже при ошибке.
    """
    output = data.get("intent_output")
    if not isinstance(output, dict):
        return "", None
    raw_id = output.get("conversation_id")
    conversation_id = raw_id if isinstance(raw_id, str) and raw_id != "" else None
    response = output.get("response")
    if not isinstance(response, dict):
        return "", conversation_id
    speech_block = response.get("speech")
    if not isinstance(speech_block, dict):
        return "", conversation_id
    plain = speech_block.get("plain")
    if not isinstance(plain, dict):
        return "", conversation_id
    text = plain.get("speech")
    if not isinstance(text, str):
        return "", conversation_id
    return text, conversation_id


def speech_from_intent_end(events: list[PipelineEvent]) -> tuple[str, str | None]:
    """Взять речь последнего события intent-end.

    Другие события запуска, в том числе error, речь не подменяют.
    """
    speech = ""
    conversation_id: str | None = None
    for event in events:
        if event.type != PipelineEventType.INTENT_END or not isinstance(
            event.data, dict
        ):
            continue
        speech, conversation_id = _plain_speech(event.data)
    return speech, conversation_id


async def async_run_pipeline(
    hass: HomeAssistant,
    text: str,
    conversation_id: str | None,
    context: Context,
    pipeline_id: str,
) -> PipelineReply | None:
    """Один раз запустить конвейер со стадии INTENT до INTENT.

    Распознавание и синтез не входят в запуск: обе границы равны INTENT.
    В PipelineInput уходит только текст фразы. Системный текст, история,
    tools, current_time и text.format сюда не копируются. Язык отдельно
    не передаётся, его берёт конвейер. Идентификатор диалога приходит
    из таймера записи, не из тела запроса. None — конвейер не найден
    или запуск завершился исключением. Текст исключения в журнал не
    пишется: в нём может быть реплика.
    """
    try:
        pipeline = async_get_pipeline(hass, pipeline_id)
    except KeyError, PipelineNotFound:
        return None

    events: list[PipelineEvent] = []

    def on_event(event: PipelineEvent) -> None:
        events.append(event)

    try:
        with async_get_chat_session(hass, conversation_id) as session:
            pipeline_input = PipelineInput(
                session=session,
                intent_input=text,
                run=PipelineRun(
                    hass,
                    context=context,
                    pipeline=pipeline,
                    start_stage=PipelineStage.INTENT,
                    end_stage=PipelineStage.INTENT,
                    event_callback=on_event,
                ),
            )
            await pipeline_input.execute(validate=True)
            speech, event_conversation_id = speech_from_intent_end(events)
            chosen = (
                event_conversation_id
                if event_conversation_id is not None
                else session.conversation_id
            )
            return PipelineReply(speech=speech, conversation_id=chosen)
    except BaseException as err:
        if isinstance(err, _PASSTHROUGH_FAILURES):
            raise
        _LOGGER.debug(
            "Запуск конвейера завершился ошибкой %s",
            type(err).__name__,
        )
        return None
