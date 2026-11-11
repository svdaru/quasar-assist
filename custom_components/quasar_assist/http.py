"""HTTP-адреса Responses и списка записей."""

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from aiohttp import web
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

from .classify import match_route
from .const import (
    DOMAIN,
    MAX_REQUEST_BODY_BYTES,
    MODELS_URL,
    MODELS_VIEW_NAME,
    PHRASE_EMPTY,
    PHRASE_FAILED,
    PHRASE_NOT_FOUND,
    RESPONSES_URL,
    RESPONSES_VIEW_NAME,
)
from .pipeline import _PASSTHROUGH_FAILURES, async_run_pipeline
from .runtime import (
    QuasarAssistRuntimeData,
    close_dialogue,
    conversation_ttl,
    hold_chat_session,
    live_conversation_id,
    pipeline_id_of,
    refresh_pipeline,
    remember_success,
    utcnow,
)

_LOGGER = logging.getLogger(__name__)

_MISSING = object()


@dataclass(slots=True)
class _TurnLog:
    """Поля одной отладочной строки хода запроса.

    Реплика пишется, только если строка прошла проверку формы. Речь —
    только если конвейер её вернул. Маршрут — литерал llm или yandex.
    Текст исключения сюда не входит, только имя типа.
    """

    phrase: str | None = None
    speech: str | None = None
    route: str | None = None
    error_type: str | None = None

    def write(self) -> None:
        """Записать одну строку DEBUG. Тело, история и системный текст не входят."""
        _LOGGER.debug(
            "Ход запроса реплика=%s речь=%s маршрут=%s сбой=%s",
            "" if self.phrase is None else self.phrase,
            "" if self.speech is None else self.speech,
            "" if self.route is None else self.route,
            "" if self.error_type is None else self.error_type,
        )


def classify_branch(body: dict[str, Any]) -> bool:
    """Ветка включена, только если первое system — строка ровно classify.

    Пробелы вокруг, другая строка и нестроковое содержание ветку не включают.
    Более позднее system не смотрится, если первое уже найдено. Нестроковое
    system само по себе не делает тело чужой формой.
    """
    raw_input = body.get("input")
    if not isinstance(raw_input, list):
        return False
    for item in raw_input:
        if not isinstance(item, dict) or item.get("role") != "system":
            continue
        return item.get("content") == "classify"
    return False


def empty_failure() -> web.Response:
    """Пустой HTTP 500 без JSON: маршрут выбирает on_error колонки."""
    return web.Response(status=500, body=b"")


@dataclass(frozen=True, slots=True)
class ParsedBody:
    """Разобранное тело: модель и текст последнего user без изменений."""

    model: str
    text: str | None


def parse_body(body: object) -> ParsedBody | None:
    """Разобрать тело Responses.

    None означает чужую форму: нет списка input или последнее сообщение user
    не является строкой. Строка, прошедшая проверку, не меняется.
    """
    if not isinstance(body, dict):
        return None
    raw_input = body.get("input", _MISSING)
    if not isinstance(raw_input, list):
        return None

    last_user: dict[str, Any] | None = None
    for item in raw_input:
        if isinstance(item, dict) and item.get("role") == "user":
            last_user = item

    text: str | None
    if last_user is None:
        text = None
    else:
        content = last_user.get("content", _MISSING)
        if not isinstance(content, str):
            return None
        text = content

    raw_model = body.get("model")
    model = raw_model if isinstance(raw_model, str) else ""
    return ParsedBody(model=model, text=text)


async def async_load_json_body(request: web.Request) -> object:
    """Прочитать JSON, если тело не больше предела.

    Content-Length выше предела — отказ до чтения. Иначе чтение через
    content.read, не больше предела плюс один байт: request.read без
    длины буферизует всё тело. ValueError — битый JSON или размер выше
    предела. Тело и токен в журнал не пишутся. Строка, которую затем
    примет разбор формы, здесь не меняется.
    """
    length = request.content_length
    if length is not None and length > MAX_REQUEST_BODY_BYTES:
        raise ValueError
    # Потоково, не больше предела плюс один байт. Дальше тело не читаем.
    cap = MAX_REQUEST_BODY_BYTES + 1
    chunks: list[bytes] = []
    received = 0
    while received < cap:
        block = await request.content.read(cap - received)
        if not block:
            break
        chunks.append(block)
        received += len(block)
    if received > MAX_REQUEST_BODY_BYTES:
        raise ValueError
    parsed: object = json.loads(b"".join(chunks))
    return parsed


def completed_body(text: str) -> dict[str, Any]:
    """Ответ мосту: завершённое сообщение без вызова инструментов."""
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": text},
                ],
            }
        ],
    }


def runtime_of(entry: ConfigEntry) -> QuasarAssistRuntimeData | None:
    """Вернуть состояние диалога, если запись загружена."""
    runtime = getattr(entry, "runtime_data", None)
    if isinstance(runtime, QuasarAssistRuntimeData):
        return runtime
    return None


def find_entry(hass: HomeAssistant, model: str) -> ConfigEntry | None:
    """Найти запись домена по entry_id из поля model."""
    if model == "":
        return None
    entry = hass.config_entries.async_get_entry(model)
    if entry is None or entry.domain != DOMAIN:
        return None
    return entry


def model_rows(hass: HomeAssistant) -> list[dict[str, str]]:
    """Собрать модели из config entries, по возрастанию id."""
    rows: list[dict[str, str]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        title = entry.title
        name = title if title != "" else entry.entry_id
        rows.append({"id": entry.entry_id, "name": name})
    rows.sort(key=lambda row: row["id"])
    return rows


class ResponsesView(HomeAssistantView):
    """POST /api/quasar_assist/v1/responses."""

    url = RESPONSES_URL
    name = RESPONSES_VIEW_NAME
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Принять Responses и вернуть речь, маршрут или короткую фразу.

        Порядок один: чужая форма, неизвестная запись, пустая фраза,
        затем ветка маршрута или прежний конвейер. Ветка не берёт замок
        и не зависит от конвейера. Без неё замок держится от проверки
        конвейера до remember_success или close_dialogue. На запрос
        одна отладочная строка. Заголовок авторизации здесь не читается.
        """
        turn = _TurnLog()
        try:
            return await self._complete(request, turn)
        finally:
            turn.write()

    async def _complete(self, request: web.Request, turn: _TurnLog) -> web.Response:
        """Пройти порядок отказов и вернуть ответ. Журнал пишет post."""
        try:
            body = await async_load_json_body(request)
        except ValueError:
            return self.json({"error": "invalid_request"}, status_code=400)

        parsed = parse_body(body)
        if parsed is None or not isinstance(body, dict):
            return self.json({"error": "invalid_request"}, status_code=400)
        text = parsed.text
        if isinstance(text, str):
            turn.phrase = text

        hass = cast(HomeAssistant, request.app["hass"])
        user = cast(User, request["hass_user"])
        branch = classify_branch(body)

        entry = find_entry(hass, parsed.model)
        if entry is None:
            return self.json(completed_body(PHRASE_NOT_FOUND))

        if text is None or text == "":
            if branch:
                return empty_failure()
            return self.json(completed_body(PHRASE_EMPTY))

        if branch:
            return await self._route(hass, text, turn)

        runtime = runtime_of(entry)
        if runtime is None:
            return self.json(completed_body(PHRASE_FAILED))

        # Замок лежит на runtime записи и держится до выхода из блока.
        async with runtime.request_lock:
            if not refresh_pipeline(hass, entry):
                close_dialogue(runtime)
                return self.json(completed_body(PHRASE_FAILED))

            context = Context(user_id=user.id)
            conversation_id = live_conversation_id(
                runtime, conversation_ttl(entry), utcnow()
            )
            reply = await async_run_pipeline(
                hass,
                text,
                conversation_id,
                context,
                pipeline_id_of(entry),
            )
            if reply is None or reply.speech == "":
                if reply is not None and reply.error_type is not None:
                    turn.error_type = reply.error_type
                close_dialogue(runtime)
                return self.json(completed_body(PHRASE_FAILED))

            turn.speech = reply.speech
            remember_success(runtime, reply.conversation_id, utcnow())
            hold_chat_session(hass, entry)
            return self.json(completed_body(reply.speech))

    async def _route(
        self, hass: HomeAssistant, text: str, turn: _TurnLog
    ) -> web.Response:
        """Вернуть JSON маршрута. Нет агента или прочее исключение — пустой 500.

        Конвейер, замок и таймер диалога здесь не используются. Отмена
        задачи и останов интерпретатора не становятся пустым 500.
        """
        try:
            route = await match_route(hass, text)
        except BaseException as err:
            if isinstance(err, _PASSTHROUGH_FAILURES):
                raise
            turn.error_type = type(err).__name__
            return empty_failure()
        turn.route = route
        return self.json(
            completed_body(json.dumps({"route": route}, ensure_ascii=False))
        )


class ModelsView(HomeAssistantView):
    """GET /api/quasar_assist/v1/models."""

    url = MODELS_URL
    name = MODELS_VIEW_NAME
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Отдать записи домена. Конвейер не запускается, таймеры не меняются."""
        hass = cast(HomeAssistant, request.app["hass"])
        return self.json({"data": model_rows(hass)})
