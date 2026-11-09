"""Состояние диалога записи: таймер и доступность конвейера."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.chat_session import (
    CONVERSATION_TIMEOUT,
    DATA_CHAT_SESSION,
    ChatSession,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONVERSATION_TTL,
    CONF_PIPELINE_ID,
    DEFAULT_CONVERSATION_TTL,
    DOMAIN,
    ISSUE_PIPELINE_UNAVAILABLE,
    LEGACY_ISSUE_ASSISTANT_UNAVAILABLE,
)
from .pipeline import pipeline_is_available

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class QuasarAssistRuntimeData:
    """Диалог одной записи. Текст реплик и токен сюда не пишутся.

    request_lock сериализует POST только этой записи: от проверки
    конвейера до remember_success или close_dialogue. Другая запись
    ждёт свой замок и идёт параллельно.
    """

    conversation_id: str | None = None
    last_success: datetime | None = None
    pipeline_unavailable: bool = False
    session_hold_unsub: CALLBACK_TYPE | None = None
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def utcnow() -> datetime:
    """Текущий момент для срока диалога."""
    return dt_util.utcnow()


def issue_id(entry_id: str) -> str:
    """Идентификатор repair для пропавшего конвейера записи."""
    return f"{ISSUE_PIPELINE_UNAVAILABLE}_{entry_id}"


def legacy_issue_id(entry_id: str) -> str:
    """Идентификатор repair версии 0.1.0 для этой записи.

    Новый код таким идентификатором repair не создаёт.
    """
    return f"{LEGACY_ISSUE_ASSISTANT_UNAVAILABLE}_{entry_id}"


def pipeline_id_of(entry: ConfigEntry) -> str:
    """Идентификатор конвейера из данных записи.

    Отсутствие ключа — достаточный признак, что конвейер не выбран.
    Прежнее поле записи при этом не читается и целью не становится.
    """
    raw = entry.data.get(CONF_PIPELINE_ID)
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
    """Закрыть сохранённый диалог и перестать удерживать его сессию."""
    _cancel_session_hold(runtime)
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


def delete_legacy_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Снять repair версии 0.1.0, если он ещё висит на записи.

    Это чужой идентификатор. Новый repair пропавшего конвейера здесь
    не создаётся и не снимается.
    """
    ir.async_delete_issue(hass, DOMAIN, legacy_issue_id(entry_id))


def _cancel_session_hold(runtime: QuasarAssistRuntimeData) -> None:
    """Снять отложенное обновление метки сессии."""
    unsub = runtime.session_hold_unsub
    runtime.session_hold_unsub = None
    if unsub is not None:
        unsub()


def release_chat_session(entry: ConfigEntry) -> None:
    """Прекратить удержание сессии Assist этой записи."""
    runtime = getattr(entry, "runtime_data", None)
    if isinstance(runtime, QuasarAssistRuntimeData):
        _cancel_session_hold(runtime)


def _stored_chat_session(
    hass: HomeAssistant, conversation_id: str
) -> ChatSession | None:
    """Вернуть уже открытую сессию. Новую здесь не создавать."""
    sessions = hass.data.get(DATA_CHAT_SESSION)
    if not isinstance(sessions, dict):
        return None
    found = sessions.get(conversation_id)
    if isinstance(found, ChatSession):
        return found
    return None


def _refresh_delay(
    runtime: QuasarAssistRuntimeData,
    conversation_id: str,
    ttl_seconds: int,
    now: datetime,
) -> timedelta | None:
    """Интервал до следующего обновления метки, пока таймер записи жив.

    None — диалог уже закрыт или срок записи истёк: метку двигать нельзя.
    Интервал короче общей очистки сессий Assist и не выходит за срок записи.
    Сам срок записи здесь не укорачивается.
    """
    last_success = runtime.last_success
    if (
        last_success is None
        or live_conversation_id(runtime, ttl_seconds, now) != conversation_id
    ):
        return None
    remaining = last_success + timedelta(seconds=ttl_seconds) - now
    half_timeout = CONVERSATION_TIMEOUT / 2
    if remaining < half_timeout:
        return remaining
    return half_timeout


def hold_chat_session(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Держать сессию Assist того же диалога, пока жив таймер записи.

    async_get_chat_session этой версии не принимает срок жизни: очистка
    одна на все сессии. Пока таймер записи жив, обновляется метка
    активности уже открытой сессии, и Assist не снимает её раньше срока
    записи. Удержание прекращается, когда диалог закрыт, срок записи
    истёк, сессия уже снята, запись выгружена или удалена. Дольше
    таймера записи метка не обновляется.
    """
    runtime = getattr(entry, "runtime_data", None)
    if not isinstance(runtime, QuasarAssistRuntimeData):
        return
    _cancel_session_hold(runtime)
    conversation_id = runtime.conversation_id
    if conversation_id is None:
        return

    @callback
    def _refresh(now: datetime) -> None:
        runtime.session_hold_unsub = None
        delay = _refresh_delay(
            runtime,
            conversation_id,
            conversation_ttl(entry),
            now,
        )
        if delay is None:
            return
        session = _stored_chat_session(hass, conversation_id)
        if session is None:
            return
        session.async_updated()
        runtime.session_hold_unsub = async_call_later(hass, delay, _refresh)

    delay = _refresh_delay(
        runtime,
        conversation_id,
        conversation_ttl(entry),
        utcnow(),
    )
    if delay is None:
        return
    runtime.session_hold_unsub = async_call_later(hass, delay, _refresh)


def create_unavailable_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Поднять repair пропавшего конвейера. Журнал этой функцией не пишется."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_PIPELINE_UNAVAILABLE,
        translation_placeholders={"entry_id": entry.entry_id},
    )


def refresh_pipeline(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Проверить конвейер записи, один раз записать потерю и восстановление.

    Пока конвейер недоступен, висит repair. Когда он доступен, repair снимается
    и после новой загрузки: флаг в чистом состоянии сам по себе issue не хранит.
    Восстановление пишется один раз, если флаг был поднят или repair уже был
    в реестре. Токен и текст реплики в журнал не пишутся.
    """
    runtime = entry.runtime_data
    if not isinstance(runtime, QuasarAssistRuntimeData):
        return False

    repair = issue_id(entry.entry_id)
    available = pipeline_is_available(hass, pipeline_id_of(entry))
    if not available:
        if not runtime.pipeline_unavailable:
            _LOGGER.warning(
                "Конвейер записи %s недоступен",
                entry.entry_id,
            )
            runtime.pipeline_unavailable = True
            create_unavailable_issue(hass, entry)
        return False

    was_unavailable = runtime.pipeline_unavailable
    issue_present = ir.async_get(hass).async_get_issue(DOMAIN, repair) is not None
    if was_unavailable or issue_present:
        _LOGGER.info(
            "Конвейер записи %s снова доступен",
            entry.entry_id,
        )
    runtime.pipeline_unavailable = False
    ir.async_delete_issue(hass, DOMAIN, repair)
    return True
