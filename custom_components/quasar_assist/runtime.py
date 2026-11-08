"""Состояние диалога записи: таймер и доступность ассистента."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .agent import agent_is_available
from .const import (
    CONF_AGENT_ID,
    CONF_CONVERSATION_TTL,
    DEFAULT_CONVERSATION_TTL,
    DOMAIN,
    ISSUE_ASSISTANT_UNAVAILABLE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class QuasarAssistRuntimeData:
    """Диалог одной записи. Текст реплик и токен сюда не пишутся."""

    conversation_id: str | None = None
    last_success: datetime | None = None
    assistant_unavailable: bool = False


def utcnow() -> datetime:
    """Текущий момент для срока диалога."""
    return dt_util.utcnow()


def issue_id(entry_id: str) -> str:
    """Идентификатор repair для пропавшего ассистента записи."""
    return f"{ISSUE_ASSISTANT_UNAVAILABLE}_{entry_id}"


def agent_id_of(entry: ConfigEntry) -> str:
    """Идентификатор ассистента из данных записи."""
    raw = entry.data.get(CONF_AGENT_ID)
    if isinstance(raw, str):
        return raw
    return ""


def conversation_ttl(entry: ConfigEntry) -> int:
    """Срок диалога записи в секундах. Чужое значение заменяется на 90."""
    raw = entry.data.get(CONF_CONVERSATION_TTL, DEFAULT_CONVERSATION_TTL)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return DEFAULT_CONVERSATION_TTL


def live_conversation_id(
    runtime: QuasarAssistRuntimeData,
    ttl_seconds: int,
    now: datetime,
) -> str | None:
    """Вернуть живой идентификатор диалога или None, если срок истёк.

    Продолжение решает только таймер. Слова фразы сюда не входят.
    Срок считается от последнего успешного ответа и не включает границу.
    """
    if runtime.conversation_id is None or runtime.last_success is None:
        return None
    if now - runtime.last_success >= timedelta(seconds=ttl_seconds):
        return None
    return runtime.conversation_id


def close_dialogue(runtime: QuasarAssistRuntimeData) -> None:
    """Закрыть сохранённый диалог записи."""
    runtime.conversation_id = None
    runtime.last_success = None


def remember_success(
    runtime: QuasarAssistRuntimeData,
    conversation_id: str | None,
    now: datetime,
) -> None:
    """Оставить диалог и продлить срок после непустой речи."""
    runtime.conversation_id = conversation_id
    runtime.last_success = now


def create_unavailable_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Поднять repair пропавшего ассистента. Журнал этой функцией не пишется."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_ASSISTANT_UNAVAILABLE,
        translation_placeholders={"entry_id": entry.entry_id},
    )


def refresh_assistant(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Проверить ассистента записи, один раз записать потерю и восстановление.

    Пока ассистент недоступен, висит repair. Когда он доступен, repair снимается
    и после новой загрузки: флаг в чистом состоянии сам по себе issue не хранит.
    Восстановление пишется один раз, если флаг был поднят или repair уже был в реестре.
    Токен и текст реплики в журнал не пишутся.
    """
    runtime = entry.runtime_data
    if not isinstance(runtime, QuasarAssistRuntimeData):
        return False

    repair = issue_id(entry.entry_id)
    available = agent_is_available(hass, agent_id_of(entry))
    if not available:
        if not runtime.assistant_unavailable:
            _LOGGER.warning(
                "Голосовой ассистент записи %s недоступен",
                entry.entry_id,
            )
            runtime.assistant_unavailable = True
            create_unavailable_issue(hass, entry)
        return False

    was_unavailable = runtime.assistant_unavailable
    issue_present = ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    if was_unavailable or issue_present:
        _LOGGER.info(
            "Голосовой ассистент записи %s снова доступен",
            entry.entry_id,
        )
    runtime.assistant_unavailable = False
    ir.async_delete_issue(hass, DOMAIN, repair)
    return True
