"""Адрес Responses и список записей."""

import asyncio
import json
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiohttp import ClientSession
from homeassistant.components.assist_pipeline.models import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.conversation.models import (
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.chat_session import CONVERSATION_TIMEOUT, DATA_CHAT_SESSION
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.quasar_assist.const import (
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
from custom_components.quasar_assist.http import (
    ModelsView,
    ResponsesView,
    parse_body,
)
from custom_components.quasar_assist.pipeline import speech_from_intent_end
from custom_components.quasar_assist.runtime import (
    QuasarAssistRuntimeData,
    close_dialogue,
    hold_chat_session,
    remember_success,
)
from tests.common import (
    Clock,
    PipelineCapture,
    SpeechEngine,
    assert_completed,
    assert_same_entry,
    async_add_entry,
    async_create_pipeline,
    async_delete_pipeline,
    async_prepare_assist,
    intent_speech,
    patch_clock,
    patch_execute,
    patch_session_ids,
    responses_body,
)


def test_view_names_are_stable() -> None:
    """Имя представления не собирается заново на каждый запрос."""
    assert ResponsesView.url == RESPONSES_URL
    assert ModelsView.url == MODELS_URL
    assert ResponsesView.name == RESPONSES_VIEW_NAME
    assert ModelsView.name == MODELS_VIEW_NAME
    assert ResponsesView.requires_auth is True
    assert ModelsView.requires_auth is True
    assert isinstance(ResponsesView.name, str)
    assert ResponsesView.name != ""


async def test_phrase_is_sent_unchanged(
    hass: HomeAssistant,
    hass_client: Any,
    hass_admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-01: фраза уходит один раз, речь берётся из intent-end."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id)
    client: ClientSession = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет"),
    )
    assert response.status == 200
    assert len(capture.inputs) == 1
    assert len(engine.calls) == 1
    pipeline_input = capture.inputs[0]
    assert pipeline_input.intent_input == "включи свет"
    assert pipeline_input.run.start_stage == PipelineStage.INTENT
    assert pipeline_input.run.end_stage == PipelineStage.INTENT
    assert pipeline_input.stt_stream is None
    assert pipeline_input.tts_input is None
    assert pipeline_input.conversation_extra_system_prompt is None
    assert pipeline_input.device_id is None
    assert pipeline_input.satellite_id is None
    assert pipeline_input.run.pipeline.id == pipeline_id
    assert pipeline_input.run.language == "en"
    assert isinstance(pipeline_input.run.context, Context)
    assert pipeline_input.run.context.user_id == hass_admin_user.id
    assert sessions == [None]
    assert "from-client" not in sessions
    assert engine.calls[0][0] == "включи свет"
    assert engine.calls[0][2] is None
    stages = {event.type for event in capture.events[0]}
    assert "stt-start" not in stages
    assert "tts-start" not in stages
    assert "wake_word-start" not in stages
    speech = intent_speech(capture.events[0])
    assert_completed(await response.json(), speech)
    assert speech == "Включила свет"
    runtime: QuasarAssistRuntimeData = entry.runtime_data
    assert runtime.conversation_id == engine.calls[0][1]
    assert runtime.conversation_id is not None
    assert runtime.last_success is not None


async def test_prefixed_phrase_is_not_trimmed(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N05: обращение, которое мост не срезал, уходит дословно."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "Львовна, включи свет", noise=False),
    )
    assert response.status == 200
    assert capture.inputs[0].intent_input == "Львовна, включи свет"


async def test_whitespace_phrase_is_not_trimmed(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пробелы во фразе не считаются пустотой и не срезаются."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, " включи свет ", noise=False),
    )
    assert response.status == 200
    assert capture.inputs[0].intent_input == " включи свет "


async def test_error_type_with_speech_extends_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-02: непустая речь при ошибочном типе ответа остаётся успехом."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.error_type = True
    engine.speech = "Включила свет"
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    clock = Clock(dt_util.utcnow())
    patch_clock(monkeypatch, clock)
    entry = await async_add_entry(hass, pipeline_id, ttl=90)
    client = await hass_client()
    first = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет"),
    )
    assert first.status == 200
    assert_completed(await first.json(), "Включила свет")
    ended = next(
        event
        for event in capture.events[0]
        if getattr(event, "type", None) == "intent-end"
    )
    assert isinstance(ended.data, dict)
    assert ended.data["intent_output"]["response"]["response_type"] == "error"
    clock.moment = clock.moment + timedelta(seconds=10)
    second = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "А в спальне", noise=False),
    )
    assert second.status == 200
    assert capture.inputs[1].intent_input == "А в спальне"
    assert sessions[0] is None
    assert sessions[1] == engine.calls[0][1]
    runtime: QuasarAssistRuntimeData = entry.runtime_data
    assert runtime.conversation_id == engine.calls[1][1]
    assert runtime.conversation_id == sessions[1]
    assert runtime.last_success == clock.moment
    assert len(capture.inputs) == 2


async def test_two_entries_do_not_share_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03: разные записи запускают свои конвейеры и не делят диалог."""
    bedroom_pipeline, engine = await async_create_pipeline(hass, "Спальня")
    kitchen_pipeline, _kitchen_engine = await async_create_pipeline(
        hass, "Кухня", engine
    )
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    bedroom = await async_add_entry(
        hass,
        bedroom_pipeline,
        title="Спальня",
        entry_id="entry-bedroom",
    )
    kitchen = await async_add_entry(
        hass,
        kitchen_pipeline,
        title="Кухня",
        entry_id="entry-kitchen",
    )
    client = await hass_client()
    await client.post(
        RESPONSES_URL,
        json=responses_body(bedroom.entry_id, "включи свет", noise=False),
    )
    await client.post(
        RESPONSES_URL,
        json=responses_body(kitchen.entry_id, "включи чайник", noise=False),
    )
    await client.post(
        RESPONSES_URL,
        json=responses_body(bedroom.entry_id, "А в спальне", noise=False),
    )
    assert capture.inputs[0].run.pipeline.id == bedroom_pipeline
    assert capture.inputs[1].run.pipeline.id == kitchen_pipeline
    assert capture.inputs[2].run.pipeline.id == bedroom_pipeline
    assert sessions[0] is None
    assert sessions[1] is None
    assert sessions[2] == bedroom.runtime_data.conversation_id
    assert kitchen.runtime_data.conversation_id != bedroom.runtime_data.conversation_id
    assert engine.calls[0][0] == "включи свет"
    assert engine.calls[1][0] == "включи чайник"


def _lock_waiter_count(lock: asyncio.Lock) -> int:
    """Сколько задач уже стоят в очереди этого замка."""
    waiters = getattr(lock, "_waiters", None)
    if isinstance(waiters, Collection):
        return len(waiters)
    return 0


async def _wait_until(ready: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Крутить цикл, пока ready не станет истинным или не выйдет срок."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not ready():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0)
    return True


def _own_log(caplog: pytest.LogCaptureFixture) -> str:
    """Текст журнала интеграции, без чужих логгеров."""
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    )


def _own_log_above_debug(caplog: pytest.LogCaptureFixture) -> str:
    """Журнал интеграции выше отладки. Реплика туда не входит."""
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
        and record.levelno > logging.DEBUG
    )


async def test_same_entry_posts_do_not_overlap_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Два POST одной записи не входят в конвейер одновременно.

    Пока первый запуск ждёт внутри конвейера, второй стоит на замке
    записи и в конвейер не входит. Чужая форма, неизвестная запись и
    пустая фраза замок не берут и живой диалог не меняют. Второй запуск
    начинается после remember и удержания первого. Итог — закрытый
    диалог второго запроса, а не разорванное состояние.
    """
    success_text = "секрет-перекрытие-успех"
    failure_text = "секрет-перекрытие-сбой"
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    entry = await async_add_entry(hass, pipeline_id)
    moment = dt_util.utcnow()
    entry.runtime_data.conversation_id = "live"
    entry.runtime_data.last_success = moment
    lock = entry.runtime_data.request_lock
    assert isinstance(lock, asyncio.Lock)

    order: list[tuple[str, str | None]] = []
    release_first = asyncio.Event()
    first_inside = asyncio.Event()
    original = engine.async_process

    async def gated(user_input: ConversationInput) -> ConversationResult:
        order.append(("enter", user_input.text))
        if user_input.text == success_text:
            engine.speech = "Готово"
            first_inside.set()
            await release_first.wait()
        else:
            engine.speech = ""
        return await original(user_input)

    def spy_remember(
        runtime: QuasarAssistRuntimeData,
        conversation_id: str | None,
        now: datetime,
    ) -> None:
        order.append(("remember", conversation_id))
        remember_success(runtime, conversation_id, now)

    def spy_hold(hass: HomeAssistant, held_entry: ConfigEntry) -> None:
        runtime = getattr(held_entry, "runtime_data", None)
        held = (
            runtime.conversation_id
            if isinstance(runtime, QuasarAssistRuntimeData)
            else None
        )
        order.append(("hold", held))
        hold_chat_session(hass, held_entry)

    def spy_close(runtime: QuasarAssistRuntimeData) -> None:
        order.append(("close", runtime.conversation_id))
        close_dialogue(runtime)

    monkeypatch.setattr(engine, "async_process", gated)
    monkeypatch.setattr(
        "custom_components.quasar_assist.http.remember_success",
        spy_remember,
    )
    monkeypatch.setattr(
        "custom_components.quasar_assist.http.hold_chat_session",
        spy_hold,
    )
    monkeypatch.setattr(
        "custom_components.quasar_assist.http.close_dialogue",
        spy_close,
    )
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    client = await hass_client()

    def failure_entered() -> bool:
        return ("enter", failure_text) in order

    first_task = asyncio.create_task(
        client.post(
            RESPONSES_URL,
            json=responses_body(entry.entry_id, success_text, noise=False),
        )
    )
    second_task: asyncio.Task[Any] | None = None
    try:
        assert await _wait_until(first_inside.is_set)
        assert order == [("enter", success_text)]
        assert lock.locked()
        second_task = asyncio.create_task(
            client.post(
                RESPONSES_URL,
                json=responses_body(entry.entry_id, failure_text, noise=False),
            )
        )
        assert await _wait_until(
            lambda: failure_entered() or _lock_waiter_count(lock) >= 1
        )
        assert not failure_entered()
        assert lock.locked()
        assert _lock_waiter_count(lock) >= 1

        empty = await asyncio.wait_for(
            client.post(
                RESPONSES_URL,
                json=responses_body(entry.entry_id, "", noise=False),
            ),
            timeout=2,
        )
        unknown = await asyncio.wait_for(
            client.post(
                RESPONSES_URL,
                json=responses_body("нет-такой-записи", failure_text, noise=False),
            ),
            timeout=2,
        )
        foreign = await asyncio.wait_for(
            client.post(RESPONSES_URL, json={"model": entry.entry_id}),
            timeout=2,
        )
        assert empty.status == 200
        assert_completed(await empty.json(), PHRASE_EMPTY)
        assert unknown.status == 200
        assert_completed(await unknown.json(), PHRASE_NOT_FOUND)
        assert foreign.status == 400
        assert entry.runtime_data.conversation_id == "live"
        assert entry.runtime_data.last_success == moment
        assert order == [("enter", success_text)]
        assert _lock_waiter_count(lock) >= 1
        release_first.set()
    except Exception:
        release_first.set()
        pending: list[asyncio.Task[Any]] = [first_task]
        if second_task is not None:
            pending.append(second_task)
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=2,
        )
        raise

    assert second_task is not None
    success_response, failure_response = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=2,
    )
    assert success_response.status == 200
    assert_completed(await success_response.json(), "Готово")
    assert failure_response.status == 200
    assert_completed(await failure_response.json(), PHRASE_FAILED)
    assert [item[0] for item in order] == [
        "enter",
        "remember",
        "hold",
        "enter",
        "close",
    ]
    assert order[0] == ("enter", success_text)
    remembered = order[1][1]
    assert remembered not in (None, "")
    assert order[2] == ("hold", remembered)
    assert order[3] == ("enter", failure_text)
    assert order[4] == ("close", remembered)
    assert [call[0] for call in engine.calls] == [success_text, failure_text]
    assert engine.calls[1][1] == remembered
    assert entry.runtime_data.conversation_id is None
    assert entry.runtime_data.last_success is None
    assert entry.runtime_data.session_hold_unsub is None
    own = _own_log(caplog)
    above = _own_log_above_debug(caplog)
    assert success_text not in above
    assert failure_text not in above
    assert hass_access_token not in own


async def test_two_entries_run_pipeline_at_the_same_time(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Две записи выполняют конвейер одновременно, замки у них разные."""
    bedroom_text = "секрет-параллель-спальня"
    kitchen_text = "секрет-параллель-кухня"
    engine = SpeechEngine()
    bedroom_pipeline, _same = await async_create_pipeline(hass, "Спальня", engine)
    kitchen_pipeline, _same_again = await async_create_pipeline(hass, "Кухня", engine)
    bedroom = await async_add_entry(
        hass,
        bedroom_pipeline,
        title="Спальня",
        entry_id="entry-bedroom-parallel",
    )
    kitchen = await async_add_entry(
        hass,
        kitchen_pipeline,
        title="Кухня",
        entry_id="entry-kitchen-parallel",
    )
    bedroom_lock = bedroom.runtime_data.request_lock
    kitchen_lock = kitchen.runtime_data.request_lock
    assert isinstance(bedroom_lock, asyncio.Lock)
    assert isinstance(kitchen_lock, asyncio.Lock)
    assert bedroom_lock is not kitchen_lock

    seen: list[str] = []
    release = asyncio.Event()
    both_inside = asyncio.Event()
    original = engine.async_process

    async def gated(user_input: ConversationInput) -> ConversationResult:
        seen.append(user_input.text)
        if len(seen) >= 2:
            both_inside.set()
        await release.wait()
        engine.speech = "Готово"
        return await original(user_input)

    monkeypatch.setattr(engine, "async_process", gated)
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    client = await hass_client()
    bedroom_task = asyncio.create_task(
        client.post(
            RESPONSES_URL,
            json=responses_body(bedroom.entry_id, bedroom_text, noise=False),
        )
    )
    kitchen_task = asyncio.create_task(
        client.post(
            RESPONSES_URL,
            json=responses_body(kitchen.entry_id, kitchen_text, noise=False),
        )
    )
    try:
        assert await _wait_until(both_inside.is_set)
        assert not bedroom_task.done()
        assert not kitchen_task.done()
        assert sorted(seen) == sorted([bedroom_text, kitchen_text])
        release.set()
    except Exception:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(bedroom_task, kitchen_task, return_exceptions=True),
            timeout=2,
        )
        raise

    bedroom_response, kitchen_response = await asyncio.wait_for(
        asyncio.gather(bedroom_task, kitchen_task),
        timeout=2,
    )
    assert bedroom_response.status == 200
    assert kitchen_response.status == 200
    assert_completed(await bedroom_response.json(), "Готово")
    assert_completed(await kitchen_response.json(), "Готово")
    assert bedroom.runtime_data.conversation_id is not None
    assert kitchen.runtime_data.conversation_id is not None
    assert bedroom.runtime_data.conversation_id != kitchen.runtime_data.conversation_id
    own = _own_log(caplog)
    above = _own_log_above_debug(caplog)
    assert bedroom_text not in above
    assert kitchen_text not in above
    assert hass_access_token not in own


async def test_timer_continues_any_phrase_until_it_expires(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-04 и AC-N03, AC-N04: таймер продолжает любую фразу, срок начинает новую."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    clock = Clock(dt_util.utcnow())
    patch_clock(monkeypatch, clock)
    entry = await async_add_entry(hass, pipeline_id, ttl=90)
    client = await hass_client()

    async def say(text: str) -> None:
        response = await client.post(
            RESPONSES_URL,
            json=responses_body(entry.entry_id, text, noise=False),
        )
        assert response.status == 200

    await say("включи свет")
    assert sessions[0] is None
    clock.moment = clock.moment + timedelta(seconds=89)
    await say("А в спальне")
    await say("включи телевизор")
    await say("да")
    await say("нет")
    await say("а выключи")
    assert [item.intent_input for item in capture.inputs[1:]] == [
        "А в спальне",
        "включи телевизор",
        "да",
        "нет",
        "а выключи",
    ]
    continued = sessions[1:]
    assert continued[0] is not None
    assert continued == [continued[0]] * len(continued)
    assert entry.runtime_data.conversation_id == continued[0]

    clock.moment = entry.runtime_data.last_success + timedelta(seconds=90)
    await say("да")
    assert capture.inputs[-1].intent_input == "да"
    assert sessions[-1] is None


def _chat_sessions(hass: HomeAssistant) -> dict[str, Any]:
    """Карта сессий Assist. Её нет, пока диалог ни разу не открывался."""
    found = hass.data.get(DATA_CHAT_SESSION)
    assert isinstance(found, dict)
    return found


async def _advance(hass: HomeAssistant, freezer: Any, step: timedelta) -> None:
    """Сдвинуть замороженные часы и дождаться сработавших таймеров."""
    freezer.tick(step)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_same_session_survives_assist_timeout_while_entry_timer_lives(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-04: при сроке длиннее очистки Assist фраза остаётся в той же сессии.

    Срок записи не ограничен пятью минутами. Часы проходят момент, когда
    Assist уже снял бы сессию. Следующая фраза идёт в прежний
    conversation_id, сессия не подменяется новой.
    """
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    goal = CONVERSATION_TIMEOUT + timedelta(seconds=2)
    ttl = int(goal.total_seconds()) + 60
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id, ttl=ttl)
    client = await hass_client()
    caplog.clear()
    first = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert first.status == 200
    conversation_id = entry.runtime_data.conversation_id
    assert conversation_id is not None
    assert sessions[0] is None
    original = _chat_sessions(hass)[conversation_id]
    assert original.conversation_id == conversation_id

    progress = timedelta(0)
    half = CONVERSATION_TIMEOUT / 2
    while progress < goal:
        step = half if progress + half <= goal else goal - progress
        await _advance(hass, freezer, step)
        progress += step

    assert _chat_sessions(hass)[conversation_id] is original
    second = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "А в спальне", noise=False),
    )
    assert second.status == 200
    assert capture.inputs[1].intent_input == "А в спальне"
    assert capture.inputs[1].session is original
    assert sessions[1] == conversation_id
    assert engine.calls[0][1] == conversation_id
    assert engine.calls[1][1] == conversation_id
    assert entry.runtime_data.conversation_id == conversation_id
    above = _own_log_above_debug(caplog)
    assert "включи свет" not in above
    assert "А в спальне" not in above


async def test_entry_timer_of_90_seconds_still_starts_a_new_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
) -> None:
    """Срок 90 секунд по-прежнему начинает новый диалог и не продлевает сессию."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    sessions = patch_session_ids(monkeypatch)
    entry = await async_add_entry(hass, pipeline_id, ttl=90)
    client = await hass_client()
    first = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert first.status == 200
    conversation_id = entry.runtime_data.conversation_id
    assert conversation_id is not None
    original = _chat_sessions(hass)[conversation_id]
    marked = original.last_updated

    await _advance(hass, freezer, timedelta(seconds=90))
    assert original.last_updated == marked
    assert entry.runtime_data.session_hold_unsub is None
    second = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "да", noise=False),
    )
    assert second.status == 200
    assert capture.inputs[1].intent_input == "да"
    assert sessions[1] is None
    assert engine.calls[1][1] != conversation_id
    assert entry.runtime_data.conversation_id != conversation_id


async def test_expired_entry_timer_lets_assist_drop_the_session(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
) -> None:
    """После срока записи метка больше не обновляется, и Assist снимает сессию."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id, ttl=90)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    conversation_id = entry.runtime_data.conversation_id
    assert conversation_id is not None
    await _advance(hass, freezer, timedelta(seconds=90))
    assert entry.runtime_data.session_hold_unsub is None
    await _advance(hass, freezer, CONVERSATION_TIMEOUT)
    assert conversation_id not in _chat_sessions(hass)


async def test_unload_stops_holding_the_chat_session(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
) -> None:
    """Выгрузка записи прекращает удержание, и Assist снимает сессию по своему сроку."""
    ttl = int(CONVERSATION_TIMEOUT.total_seconds()) + 600
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id, ttl=ttl)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    conversation_id = entry.runtime_data.conversation_id
    assert conversation_id is not None
    original = _chat_sessions(hass)[conversation_id]
    marked = original.last_updated
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _advance(hass, freezer, CONVERSATION_TIMEOUT / 2)
    assert original.last_updated == marked
    await _advance(hass, freezer, CONVERSATION_TIMEOUT)
    assert conversation_id not in _chat_sessions(hass)


async def test_closed_dialogue_stops_holding_the_chat_session(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
) -> None:
    """Закрытый диалог больше не обновляет метку сессии."""
    ttl = int(CONVERSATION_TIMEOUT.total_seconds()) + 600
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id, ttl=ttl)
    client = await hass_client()
    opened = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert opened.status == 200
    conversation_id = entry.runtime_data.conversation_id
    assert conversation_id is not None
    engine.speech = ""
    failed = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "выключи", noise=False),
    )
    assert failed.status == 200
    assert entry.runtime_data.conversation_id is None
    original = _chat_sessions(hass)[conversation_id]
    marked = original.last_updated
    await _advance(hass, freezer, CONVERSATION_TIMEOUT / 2)
    assert original.last_updated == marked


async def test_missing_chat_session_is_not_recreated(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
) -> None:
    """Если Assist уже снял сессию, удержание не открывает новую."""
    ttl = int(CONVERSATION_TIMEOUT.total_seconds()) + 600
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id, ttl=ttl)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    conversation_id = entry.runtime_data.conversation_id
    assert conversation_id is not None
    stored = _chat_sessions(hass)
    del stored[conversation_id]
    await _advance(hass, freezer, CONVERSATION_TIMEOUT / 2)
    assert conversation_id not in _chat_sessions(hass)
    assert entry.runtime_data.session_hold_unsub is None


async def test_models_list_entries_without_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-05: список из config entries, порядок по id, таймеры не меняются."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Общий")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    named = await async_add_entry(
        hass,
        pipeline_id,
        title="Спальня",
        entry_id="entry-b",
    )
    untitled = await async_add_entry(
        hass,
        pipeline_id,
        title="",
        entry_id="entry-a",
    )
    named.runtime_data.conversation_id = "keep"
    moment = dt_util.utcnow()
    named.runtime_data.last_success = moment
    client = await hass_client()
    response = await client.get(MODELS_URL)
    assert response.status == 200
    payload = await response.json()
    assert payload == {
        "data": [
            {"id": "entry-a", "name": "entry-a"},
            {"id": "entry-b", "name": "Спальня"},
        ]
    }
    assert capture.inputs == []
    assert named.runtime_data.conversation_id == "keep"
    assert named.runtime_data.last_success == moment
    assert untitled.runtime_data.conversation_id is None


async def test_models_empty(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустой каталог, если записей нет. Конвейер не запускается."""
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    await async_prepare_assist(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(MODELS_URL)
    assert response.status == 200
    assert await response.json() == {"data": []}
    assert capture.inputs == []


async def test_no_outgoing_language_model(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-07: запрос обрабатывается без исходящего вызова языковой модели."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    posted = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    listed = await client.get(MODELS_URL)
    assert posted.status == 200
    assert listed.status == 200
    assert len(engine.calls) == 1
    assert capture.inputs[0].conversation_extra_system_prompt is None


async def test_unknown_model_and_order(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сначала чужая форма, затем неизвестная запись, затем пустая фраза."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id, title="Спальня")
    entry.runtime_data.conversation_id = "live"
    moment = dt_util.utcnow()
    entry.runtime_data.last_success = moment
    client = await hass_client()
    foreign = MockConfigEntry(domain="light", title="Не наш домен")
    foreign.add_to_hass(hass)

    bad_form = await client.post(
        RESPONSES_URL,
        json={"model": "nope", "input": {"role": "user", "content": "включи свет"}},
    )
    assert bad_form.status == 400
    assert capture.inputs == []

    unknown = await client.post(
        RESPONSES_URL,
        json=responses_body("", "", noise=False),
    )
    assert unknown.status == 200
    assert_completed(await unknown.json(), PHRASE_NOT_FOUND)

    other_domain = await client.post(
        RESPONSES_URL,
        json=responses_body(foreign.entry_id, "включи свет", noise=False),
    )
    assert other_domain.status == 200
    assert_completed(await other_domain.json(), PHRASE_NOT_FOUND)

    missing_model = await client.post(
        RESPONSES_URL,
        json={"input": [{"role": "user", "content": "включи свет"}]},
    )
    assert missing_model.status == 200
    assert_completed(await missing_model.json(), PHRASE_NOT_FOUND)

    empty = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "", noise=False),
    )
    assert empty.status == 200
    assert_completed(await empty.json(), PHRASE_EMPTY)
    assert entry.runtime_data.conversation_id == "live"
    assert entry.runtime_data.last_success == moment

    absent = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, None, noise=True),
    )
    assert absent.status == 200
    assert_completed(await absent.json(), PHRASE_EMPTY)
    assert entry.runtime_data.conversation_id == "live"
    assert capture.inputs == []


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "x"},
        {"model": "x", "input": "включи свет"},
        {"model": "x", "input": None},
        {"model": "x", "input": [{"role": "user", "content": {"type": "text"}}]},
        {"model": "x", "input": [{"role": "user", "content": ["включи свет"]}]},
        {"model": "x", "input": [{"role": "user", "content": 1}]},
        {"model": "x", "input": [{"role": "user"}]},
        {
            "model": "x",
            "input": [
                {"role": "user", "content": "включи свет"},
                {"role": "user", "content": 1},
            ],
        },
        ["not-an-object"],
    ],
)
async def test_bad_form_is_400(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    """QA-I-01: чужая форма отвечает 400 и не запускает конвейер."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    if isinstance(payload, dict) and payload.get("model") == "x":
        payload = {**payload, "model": entry.entry_id}
    response = await client.post(RESPONSES_URL, json=payload)
    assert response.status == 400
    assert capture.inputs == []


async def test_invalid_json_is_400(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неразобранный JSON не запускает конвейер."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        data=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 400
    assert capture.inputs == []


async def test_empty_speech_and_exception_close_dialogue(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N06: пустая речь и сбой закрывают диалог, повторной отправки нет."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    engine.speech = ""
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    entry.runtime_data.conversation_id = "live"
    entry.runtime_data.last_success = dt_util.utcnow()
    client = await hass_client()
    empty = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert empty.status == 200
    assert_completed(await empty.json(), PHRASE_FAILED)
    assert len(capture.inputs) == 1
    assert len(engine.calls) == 1
    assert entry.runtime_data.conversation_id is None
    assert entry.runtime_data.last_success is None

    engine.speech = "Включила свет"
    engine.exc = RuntimeError("включи свет")
    entry.runtime_data.conversation_id = "live"
    entry.runtime_data.last_success = dt_util.utcnow()
    failed = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert failed.status == 200
    assert_completed(await failed.json(), PHRASE_FAILED)
    assert len(capture.inputs) == 2
    assert len(engine.calls) == 2
    assert entry.runtime_data.conversation_id is None


async def test_missing_pipeline_does_not_start(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N08: конвейер пропал, непустая фраза закрывает диалог без запуска."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    entry.runtime_data.conversation_id = "live"
    entry.runtime_data.last_success = dt_util.utcnow()
    await async_delete_pipeline(hass, pipeline_id)
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    assert capture.inputs == []
    assert entry.runtime_data.conversation_id is None
    assert entry.runtime_data.last_success is None


async def test_failed_start_does_not_call_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запись без рабочего конвейера на старте не запускает его."""
    await async_prepare_assist(hass)
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, "missing-pipeline")
    assert_same_entry(hass.config_entries.async_get_entry(entry.entry_id), entry)
    assert getattr(entry, "runtime_data", None) is None
    client = await hass_client()
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert response.status == 200
    assert_completed(await response.json(), PHRASE_FAILED)
    assert capture.inputs == []
    assert getattr(entry, "runtime_data", None) is None


async def test_unauthorized_is_401(
    hass: HomeAssistant,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-N07: без токена Home Assistant отвечает 401 до конвейера."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await aiohttp_client(hass.http.app)
    posted = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    listed = await client.get(MODELS_URL)
    assert posted.status == 401
    assert listed.status == 401
    assert capture.inputs == []


async def test_non_admin_token_is_accepted_and_not_stored(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    hass_read_only_user: Any,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Действительный токен не проверяется по роли и не пишется в запись."""
    assert not hass_read_only_user.is_admin
    token = hass_read_only_access_token
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client(token)
    phrase = "включи свет"
    response = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, phrase, noise=False),
    )
    assert response.status == 200
    assert capture.inputs[0].run.context.user_id == hass_read_only_user.id
    stored = json.dumps(
        {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "conversation_id": entry.runtime_data.conversation_id,
            "last_success": entry.runtime_data.last_success,
        },
        ensure_ascii=False,
        default=str,
    )
    assert token not in stored
    assert hass_access_token not in stored
    assert phrase not in stored
    assert "Bearer" not in stored


async def test_models_accepts_token_without_admin(
    hass: HomeAssistant,
    hass_client: Any,
    hass_read_only_user: Any,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-AC-01: токен без прав администратора читает список, конвейер молчит."""
    assert not hass_read_only_user.is_admin
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client(hass_read_only_access_token)
    response = await client.get(MODELS_URL)
    assert response.status == 200
    payload = await response.json()
    assert {"id": entry.entry_id, "name": entry.title} in payload["data"]
    assert capture.inputs == []


async def test_oversized_body_is_400_without_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Тело больше предела отклоняется до конвейера и не попадает в журнал."""
    assert MAX_REQUEST_BODY_BYTES >= 1024 * 1024
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    marker = "секретная-реплика-лимита"
    phrase = marker + ("x" * MAX_REQUEST_BODY_BYTES)
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    rejected = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, phrase, noise=False),
    )
    assert rejected.status == 400
    assert capture.inputs == []
    own = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    )
    assert marker not in own
    assert hass_access_token not in own

    accepted = await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, "включи свет", noise=False),
    )
    assert accepted.status == 200
    assert capture.inputs[0].intent_input == "включи свет"


async def test_oversized_chunked_body_is_400_without_pipeline(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Тело больше предела без Content-Length не доходит до конвейера и журнала."""
    pipeline_id, _engine = await async_create_pipeline(hass, "Спальня")
    capture = PipelineCapture()
    patch_execute(monkeypatch, capture)
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    marker = "секретная-реплика-без-длины"
    phrase = marker + ("x" * MAX_REQUEST_BODY_BYTES)
    payload = json.dumps(
        responses_body(entry.entry_id, phrase, noise=False),
    ).encode()
    assert len(payload) > MAX_REQUEST_BODY_BYTES
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    rejected = await client.post(
        RESPONSES_URL,
        data=payload,
        chunked=True,
        headers={"Content-Type": "application/json"},
    )
    assert rejected.status == 400
    assert rejected.request_info.headers.get("Content-Length") is None
    assert capture.inputs == []
    own = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    )
    assert marker not in own
    assert hass_access_token not in own


async def test_logs_do_not_contain_phrase_or_token(
    hass: HomeAssistant,
    hass_client: Any,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """QA-P-01: реплика только в одной отладочной строке, токен нигде."""
    pipeline_id, engine = await async_create_pipeline(hass, "Спальня")
    phrase = "секретная реплика"
    secret = "текст-исключения-не-для-журнала"
    engine.exc = RuntimeError(secret)
    engine.speech = "речь-которой-нет"
    patch_execute(monkeypatch, PipelineCapture())
    entry = await async_add_entry(hass, pipeline_id)
    client = await hass_client()
    caplog.set_level("DEBUG", logger="custom_components.quasar_assist")
    caplog.clear()
    await client.post(
        RESPONSES_URL,
        json=responses_body(entry.entry_id, phrase),
    )
    own_records = [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.quasar_assist")
    ]
    debug = [
        record.getMessage() for record in own_records if record.levelno == logging.DEBUG
    ]
    higher = [
        record.getMessage() for record in own_records if record.levelno > logging.DEBUG
    ]
    own = "\n".join(record.getMessage() for record in own_records)
    assert len(debug) == 1
    assert phrase in debug[0]
    assert secret not in own
    assert "старая реплика" not in own
    assert "Инструкции инструментов моста" not in own
    assert "речь-которой-нет" not in own
    assert phrase not in "\n".join(higher)
    assert hass_access_token not in own
    assert "Bearer" not in own
    assert "authorization" not in own.lower()


def test_parse_and_speech_edges() -> None:
    """Края разбора тела и пустой речи события intent-end."""
    assert parse_body(["x"]) is None
    parsed = parse_body(
        {
            "model": 5,
            "input": [
                {"role": "user", "content": {"bad": True}},
                {"role": "user", "content": "включи свет"},
            ],
        }
    )
    assert parsed is not None
    assert parsed.model == ""
    assert parsed.text == "включи свет"
    assert speech_from_intent_end([]) == ("", None)
    assert speech_from_intent_end(
        [PipelineEvent(PipelineEventType.ERROR, {"code": "intent-failed"})]
    ) == ("", None)
    assert speech_from_intent_end(
        [PipelineEvent(PipelineEventType.INTENT_END, None)]
    ) == ("", None)
    assert speech_from_intent_end(
        [PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": "нет"})]
    ) == ("", None)
    assert speech_from_intent_end(
        [
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {"intent_output": {"conversation_id": "conv", "response": "нет"}},
            )
        ]
    ) == ("", "conv")
    assert speech_from_intent_end(
        [
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {
                    "intent_output": {
                        "conversation_id": "",
                        "response": {"speech": "нет", "response_type": "error"},
                    }
                },
            )
        ]
    ) == ("", None)
    assert speech_from_intent_end(
        [
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {
                    "intent_output": {
                        "conversation_id": 5,
                        "response": {"speech": {"plain": "нет"}},
                    }
                },
            )
        ]
    ) == ("", None)
    assert speech_from_intent_end(
        [
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {
                    "intent_output": {
                        "response": {"speech": {"plain": {"speech": 1}}},
                    }
                },
            )
        ]
    ) == ("", None)
