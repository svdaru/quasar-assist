"""HTTP-адреса Responses и списка записей."""

import json
from dataclasses import dataclass
from typing import Any, cast

from aiohttp import web
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

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
from .pipeline import async_run_pipeline
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

_MISSING = object()


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
        """Принять Responses и вернуть речь конвейера либо короткую фразу.

        Порядок один: чужая форма, неизвестная запись, пустая фраза,
        затем неудачный старт и один запуск. Эти три отказа замок не
        берут и живой диалог не меняют. Дальше замок записи держится
        от проверки конвейера до remember_success или close_dialogue,
        включая ожидание запуска и удержание сессии. Заголовок
        авторизации здесь не читается.
        """
        try:
            body = await async_load_json_body(request)
        except ValueError:
            return self.json({"error": "invalid_request"}, status_code=400)

        hass = cast(HomeAssistant, request.app["hass"])
        user = cast(User, request["hass_user"])

        parsed = parse_body(body)
        if parsed is None:
            return self.json({"error": "invalid_request"}, status_code=400)

        entry = find_entry(hass, parsed.model)
        if entry is None:
            return self.json(completed_body(PHRASE_NOT_FOUND))

        if parsed.text is None or parsed.text == "":
            return self.json(completed_body(PHRASE_EMPTY))

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
                parsed.text,
                conversation_id,
                context,
                pipeline_id_of(entry),
            )
            if reply is None or reply.speech == "":
                close_dialogue(runtime)
                return self.json(completed_body(PHRASE_FAILED))

            remember_success(runtime, reply.conversation_id, utcnow())
            hold_chat_session(hass, entry)
            return self.json(completed_body(reply.speech))


class ModelsView(HomeAssistantView):
    """GET /api/quasar_assist/v1/models."""

    url = MODELS_URL
    name = MODELS_VIEW_NAME
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Отдать записи домена. Конвейер не запускается, таймеры не меняются."""
        hass = cast(HomeAssistant, request.app["hass"])
        return self.json({"data": model_rows(hass)})
