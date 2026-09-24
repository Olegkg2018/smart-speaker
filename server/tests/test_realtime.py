"""Приём событий от облака не должен глушить отправку звука на колонку."""

import asyncio
import contextlib
import types
from pathlib import Path

import app.realtime as realtime_mod
from app.realtime import RealtimeVoice


class _Callbacks:
    async def wait_drained(self):
        return None

    async def turn_done(self):
        return None


class _BurstConnection:
    """Соединение, у которого пачка событий уже лежит в буфере.

    Именно так ведёт себя настоящий сокет: облако присылает несколько
    десятков кусков звука разом, и каждый следующий доступен немедленно,
    без ожидания сети.
    """

    def __init__(self, count: int) -> None:
        self._left = count

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._left == 0:
            raise StopAsyncIteration
        self._left -= 1
        return object()


async def test_recv_loop_lets_the_sender_run():
    """Отправщик кадров обязан получать управление посреди пачки событий.

    Кадр звука уходит на колонку каждые 20 мс. Пока цикл приёма
    прокручивал всю пачку не уступая, отправщик просто стоял, и речь
    рвалась с опозданием до трёхсот миллисекунд.
    """
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._conn = _BurstConnection(50)
    voice._cb = _Callbacks()
    # Пачка кончается штатно (StopAsyncIteration) — это тоже теперь ведёт
    # к переподключению (см. test_recv_loop_reconnects_on_clean_close),
    # но здесь это не предмет теста: гасим, чтобы не улетала фоновая задача.
    voice._reconnecting = True

    async def _noop(_event):
        return None

    voice._on_event = _noop

    ticks = 0

    async def sender():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    pump = asyncio.create_task(sender())
    await voice._recv_loop()
    pump.cancel()

    # Без уступки соседняя задача успела бы провернуться считанные разы.
    assert ticks >= 50, f"отправщик получил управление всего {ticks} раз"


class _DyingConnection:
    """Соединение, которое рвётся исключением — сеть, неверный ключ, квота."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise RuntimeError("Your session hit the maximum duration of 60 minutes.")


class _CleanlyClosedConnection:
    """Соединение, которое обрывается штатно — без исключения.

    Так реально ведёт себя SDK openai-python: ConnectionClosedOK (именно
    это шлёт OpenAI при истечении часового лимита сессии) обрабатывается
    внутри `AsyncRealtimeConnection.__aiter__` через `except
    ConnectionClosedOK: return` — наружу ничего не долетает, `async for`
    просто заканчивается, как будто событий никогда и не было.
    """

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


async def test_recv_loop_reconnects_after_session_expires():
    """OpenAI рвёт сессию сама через час — колонка должна поднять новую.

    Без переподключения соединение остаётся мёртвым навсегда: колонка
    слушает команды, но ничего не отвечает — неотличимо от «не слышит».
    """
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._conn = _DyingConnection()
    voice._cb = _Callbacks()
    voice._reconnecting = False
    voice._history = []
    voice._summary = ""

    started = []

    async def fake_start(history, summary=""):
        started.append(history)

    voice.start = fake_start

    await voice._recv_loop()
    # _reconnect() запускается фоновой задачей — даём ей шанс выполниться.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert started, "переподключение не было запущено после разрыва сессии"


async def test_recv_loop_reconnects_on_clean_close():
    """Штатное закрытие (без исключения) обязано переподключаться так же.

    Раньше реконнект запускался только из ветки `except Exception` — а
    при истечении часового лимита SDK закрывает соединение чисто
    (`ConnectionClosedOK`), без исключения. `async for` просто
    заканчивался, `_reconnect()` не вызывался, и соединение оставалось
    мёртвым навсегда после первого же часа разговора.
    """
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._conn = _CleanlyClosedConnection()
    voice._cb = _Callbacks()
    voice._reconnecting = False
    voice._history = []
    voice._summary = ""

    async def _noop(_event):
        return None

    voice._on_event = _noop

    started = []

    async def fake_start(history, summary=""):
        started.append(history)

    voice.start = fake_start

    await voice._recv_loop()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert started, "штатное закрытие без исключения не вызвало переподключение"


def _idle_voice():
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._conn = object()
    voice._reconnecting = False
    voice._speaking = False
    voice._recv_task = None
    voice._manager = None
    voice._history = []
    voice._summary = ""
    voice._last_reconnect_announce = 0.0
    return voice


async def test_proactive_refresh_swaps_connection_when_idle():
    """В паузе разговора — можно обновиться заранее, до штатного разрыва."""
    voice = _idle_voice()
    started = []

    async def fake_start(history, summary=""):
        started.append(history)

    voice.start = fake_start

    await voice._proactive_refresh(delay_s=0)

    assert started, "обновление не запустилось в паузе разговора"
    assert voice._reconnecting is False, "флаг должен сброситься после пересборки"


async def test_proactive_refresh_does_not_cancel_itself():
    """Живой случай, самый частый из всех: `self._refresh_task` — это и
    есть задача, выполняющая `_proactive_refresh()`. Когда час истекает, она
    сама вызывает `start()`, а первая строчка `start()` раньше звала
    `self._refresh_task.cancel()` — то есть отменяла САМА СЕБЯ. Cancel()
    на текущей задаче не убивает мгновенно: CancelledError влетает на
    ближайшей же точке await (тут же, внутри start()) и тихо гасит всю
    цепочку — ни ошибки в логе, ни новой попытки, self._conn остаётся None
    навсегда. Ровно так объяснялись все «зависания на часы»: не сетевая
    заминка, а самоотмена. Тест воспроизводит один в один: настоящий
    _proactive_refresh, запущенный как настоящая self._refresh_task, с
    настоящим (не подменённым) start()."""
    voice = _voice_for_connect_tests()
    voice._conn = object()  # была рабочая сессия
    voice._recv_task = None
    voice._speaking = False
    voice._reconnecting = False
    voice._client = types.SimpleNamespace(
        realtime=types.SimpleNamespace(connect=lambda model: _FastManager())
    )

    refresh_task = asyncio.create_task(voice._proactive_refresh(delay_s=0))
    voice._refresh_task = refresh_task
    await asyncio.wait_for(refresh_task, timeout=2)

    assert voice._conn is not None, "самоотмена не должна была погасить обновление"
    assert voice._reconnecting is False

    # Уборка: start() внутри уже поставил новый recv_task/refresh_task
    # (следующий цикл обновления) — оба не нужны после теста.
    voice._recv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await voice._recv_task
    if voice._refresh_task is not None:
        voice._refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await voice._refresh_task


async def test_proactive_refresh_does_not_wait_for_old_connection_to_close():
    """Живой случай: закрытие СТАРОГО соединения зависло на минуты.

    Важно: обернуть это ожидание в asyncio.wait_for НЕ помогает — если сама
    задача проглатывает CancelledError и не завершается сразу, wait_for всё
    равно ждёт её настоящего конца, просто откладывая исключение (проверено
    отдельно: с этой оберткой тест зависал точно так же, как продакшен).
    Единственный надёжный выход — не ждать синхронно вовсе: новое соединение
    поднимается сразу, старое закрывается само по себе в фоне."""
    voice = _idle_voice()

    old_recv_task = asyncio.create_task(asyncio.sleep(999))
    voice._recv_task = old_recv_task

    manager_exited = asyncio.Event()

    class _SlowManager:
        async def __aexit__(self, *exc):
            # Дольше, чем мы готовы ждать синхронно ниже — если бы
            # _proactive_refresh ждал этого напрямую, тест бы не уложился
            # в отведённый таймаут.
            await asyncio.sleep(0.3)
            manager_exited.set()

    voice._manager = _SlowManager()

    started = []

    async def fake_start(history, summary=""):
        started.append(history)

    voice.start = fake_start

    await asyncio.wait_for(voice._proactive_refresh(delay_s=0), timeout=0.1)

    assert started, "новое соединение должно было подняться, не дожидаясь старого"
    assert not manager_exited.is_set(), "закрытие старого должно было уйти в фон, а не в этот вызов"

    # Дать фоновой уборке время дойти до конца, чтобы к концу теста ничего
    # не осталось висеть в цикле событий.
    await asyncio.sleep(0.4)
    assert manager_exited.is_set(), "но в фоне закрытие всё же должно было завершиться"


async def test_proactive_refresh_defers_while_speaking():
    """Идёт озвучка ответа — прерывать её обновлением сессии нельзя."""
    voice = _idle_voice()
    voice._speaking = True
    started = []

    async def fake_start(history, summary=""):
        started.append(history)

    voice.start = fake_start

    await voice._proactive_refresh(delay_s=0)

    assert not started, "обновление не должно прерывать текущую речь"


async def test_proactive_refresh_skips_dead_connection():
    """Соединения уже нет — обновлять нечего, реактивный путь уже сработал."""
    voice = _idle_voice()
    voice._conn = None
    started = []

    async def fake_start(history, summary=""):
        started.append(history)

    voice.start = fake_start

    await voice._proactive_refresh(delay_s=0)

    assert not started, "обновлять мёртвое соединение незачем"


async def test_end_utterance_returns_to_idle_when_connection_is_mid_reconnect():
    """Плановое переподключение раз в час обнуляет self._conn на время
    пересборки. Реплика, закончившаяся ровно в этот зазор, раньше молча
    проглатывалась — turn_done() никто не звал, и сессия оставалась в
    LISTENING навсегда. Живой случай: колонка простояла «слушаю» несколько
    часов, пока не пришло ручное вмешательство (см. app/session.py,
    _STATE_WATCHDOG_S — там теперь есть и общий сторож на этот случай)."""
    voice = _idle_voice()
    voice._conn = None
    turn_done_called = False

    class _Cb:
        async def turn_done(self):
            nonlocal turn_done_called
            turn_done_called = True

    voice._cb = _Cb()
    spoken = []

    async def fake_speak(text):
        spoken.append(text)

    voice._ctx = types.SimpleNamespace(speak=fake_speak)

    await voice.end_utterance()

    assert turn_done_called, "сессия должна вернуться в IDLE, а не зависнуть в LISTENING"
    assert spoken, "стоит хотя бы предупредить, что реплика потеряна"


async def test_reconnect_announcement_does_not_repeat_faster_than_the_gap():
    """Живой случай: соединение не поднималось несколько минут, а что-то
    (эхо колонки, шум в комнате) продолжало будить её заново каждые
    десять-пятнадцать секунд — «секунду, переподключаюсь» зациклилось.
    Turn_done() обязан звать каждый раз, а вот озвучивать — не чаще, чем
    раз в _RECONNECT_ANNOUNCE_MIN_GAP_S."""
    voice = _idle_voice()
    voice._conn = None

    class _Cb:
        async def turn_done(self):
            return None

    voice._cb = _Cb()
    spoken = []

    async def fake_speak(text):
        spoken.append(text)

    voice._ctx = types.SimpleNamespace(speak=fake_speak)

    await voice.end_utterance()
    await voice.end_utterance()
    await voice.end_utterance()

    assert len(spoken) == 1, "вторая и третья попытка не должны озвучиваться заново так быстро"


async def test_reconnect_with_retry_tries_again_after_a_transient_failure(monkeypatch):
    """Короткая сетевая заминка не должна оставлять сессию мёртвой до
    следующего часового цикла — раньше и плановое, и реактивное
    переподключение сдавались после первой же неудачи."""
    monkeypatch.setattr(realtime_mod, "_RECONNECT_BACKOFF_S", (0.0, 0.0, 0.0))
    voice = _idle_voice()
    attempts = []

    async def flaky_start(history, summary=""):
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("сеть моргнула")

    voice.start = flaky_start

    ok = await voice._reconnect_with_retry()

    assert ok is True
    assert len(attempts) == 2, "должно было хватить второй попытки"


async def test_reconnect_with_retry_gives_up_after_the_configured_attempts(monkeypatch):
    monkeypatch.setattr(realtime_mod, "_RECONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(realtime_mod, "_RECONNECT_BACKOFF_S", (0.0,))
    voice = _idle_voice()
    attempts = []

    async def always_fails(history, summary=""):
        attempts.append(1)
        raise RuntimeError("сеть недоступна")

    voice.start = always_fails

    ok = await voice._reconnect_with_retry()

    assert ok is False
    assert len(attempts) == 2, "не больше настроенного числа попыток"


class _ConnStub:
    """Достаточно от `conn`, чтобы пройти session.update() и не дать
    _recv_loop() сразу решить, что соединение уже закрылось само."""

    async def _noop(self):
        return None

    def __init__(self):
        self.session = types.SimpleNamespace(update=lambda **kw: self._noop())

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(999)  # «подключено, событий пока нет»


class _FastManager:
    """Обычное, ничем не примечательное соединение — подключается сразу."""

    async def __aenter__(self):
        return _ConnStub()

    async def __aexit__(self, *exc):
        return None


class _HangingManager:
    """Как настоящий менеджер соединения, но __aenter__ никогда не отвечает.

    Живой случай: сетевая заминка подвесила именно этот вызов на 11+ часов
    без единого исключения — start() ждал вечно, self._conn оставался None
    и это никого не удивляло: ошибки-то не было. Обернуть ожидание в
    asyncio.wait_for тоже не решение (см. коммент у _adopt_late_connect):
    если сам вызов внутри SDK не откликается на отмену, wait_for всё равно
    ждёт его настоящего конца. Единственный надёжный выход — asyncio.wait()
    без отмены: не дождались — просто перестаём ждать."""

    async def __aenter__(self):
        await asyncio.sleep(999)

    async def __aexit__(self, *exc):
        return None


def _voice_for_connect_tests():
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._refresh_task = None
    voice._connect_generation = 0
    voice._conn = None
    voice._manager = None
    voice._history = []
    voice._summary = ""
    voice._settings = types.SimpleNamespace(
        openai_realtime_model="gpt-realtime",
        realtime_retention_ratio=0.6,
        realtime_context_tokens=4000,
        openai_voice="marin",
        whisper_language="ru",
        realtime_transcribe_model="gpt-4o-transcribe",
        notes_dir=Path("/nonexistent-notes-dir-for-tests"),
    )
    return voice


async def test_start_gives_up_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr(realtime_mod, "_CONNECT_TIMEOUT_S", 0.01)
    voice = _voice_for_connect_tests()
    voice._client = types.SimpleNamespace(
        realtime=types.SimpleNamespace(connect=lambda model: _HangingManager())
    )

    raised = False
    try:
        await voice.start([], "")
    except TimeoutError:
        raised = True

    assert raised, "зависшее подключение должно превращаться в быструю ошибку"
    assert voice._conn is None
    assert voice._manager is None


async def test_late_connect_is_adopted_if_still_the_current_attempt(monkeypatch):
    """Соединение поднялось позже отведённого времени, но так и осталось
    единственной попыткой — жалко его выбрасывать только за опоздание."""
    monkeypatch.setattr(realtime_mod, "_CONNECT_TIMEOUT_S", 0.05)
    voice = _voice_for_connect_tests()

    class _SlowManager:
        async def __aenter__(self):
            await asyncio.sleep(0.2)  # дольше таймаута, но не бесконечно
            return _ConnStub()

        async def __aexit__(self, *exc):
            return None

    voice._client = types.SimpleNamespace(
        realtime=types.SimpleNamespace(connect=lambda model: _SlowManager())
    )

    with contextlib.suppress(TimeoutError):
        await voice.start([], "")
    assert voice._conn is None, "пока не должно быть готово"

    await asyncio.sleep(0.3)  # дать фоновой adopt-задаче время сработать

    assert voice._conn is not None, "опоздавшее соединение должно было прижиться"
    assert voice._recv_task is not None
    voice._recv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await voice._recv_task
    voice._refresh_task.cancel()


async def test_late_connect_is_discarded_if_something_else_already_connected(monkeypatch):
    """Пока первая попытка опаздывала, вторая (или ретрай) уже подключилась
    — опоздавшая не должна перезаписать рабочее соединение."""
    monkeypatch.setattr(realtime_mod, "_CONNECT_TIMEOUT_S", 0.05)
    voice = _voice_for_connect_tests()

    closed = asyncio.Event()

    class _SlowManager:
        async def __aenter__(self):
            await asyncio.sleep(0.2)
            return _ConnStub()

        async def __aexit__(self, *exc):
            closed.set()

    voice._client = types.SimpleNamespace(
        realtime=types.SimpleNamespace(connect=lambda model: _SlowManager())
    )

    with contextlib.suppress(TimeoutError):
        await voice.start([], "")

    # Пока первая попытка ещё в пути, кто-то другой уже подключился напрямую.
    sentinel = object()
    voice._conn = sentinel

    await asyncio.sleep(0.3)

    assert voice._conn is sentinel, "опоздавшее соединение не должно было его подвинуть"
    assert closed.is_set(), "а само оно должно было закрыться, а не повиснуть открытым"


class _FakeConn:
    def __init__(self):
        self.created_items = []
        self.responses_created = 0

        async def _create_item(item):
            self.created_items.append(item)

        async def _create_response():
            self.responses_created += 1

        self.conversation = types.SimpleNamespace(
            item=types.SimpleNamespace(create=_create_item)
        )
        self.response = types.SimpleNamespace(create=_create_response)


async def test_run_tool_drops_result_if_connection_changed_mid_dispatch(monkeypatch):
    """Долгий инструмент (например, web_search) может пережить переподключение.

    Раньше проверялось только "self._conn is None" — а свежее соединение
    после успешного _reconnect() тоже не None, просто это уже другая
    сессия, ничего не знающая про call_id из старой. Результат должен
    потеряться с предупреждением в лог, а не уйти не по адресу.
    """
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._ctx = None
    old_conn = _FakeConn()
    new_conn = _FakeConn()
    voice._conn = old_conn

    async def fake_dispatch(ctx, name, args):
        # Соединение "переподключилось" прямо во время долгого инструмента.
        voice._conn = new_conn
        return "результат"

    monkeypatch.setattr(realtime_mod, "dispatch", fake_dispatch)

    await voice._run_tools([_call("call-1", "web_search")])

    assert old_conn.created_items == [] and old_conn.responses_created == 0
    assert new_conn.created_items == [] and new_conn.responses_created == 0


def _call(call_id, name, arguments="{}"):
    return types.SimpleNamespace(
        type="function_call", call_id=call_id, name=name, arguments=arguments
    )


async def test_several_tools_in_one_response_get_one_response_create(monkeypatch):
    """«Погода и жалюзи» в одном ответе: раньше каждый инструмент сам звал
    response.create(), второй получал «already has an active response», и
    его результат не озвучивался."""
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._ctx = None
    conn = _FakeConn()
    voice._conn = conn

    async def fake_dispatch(ctx, name, args):
        await asyncio.sleep(0.01 if name == "get_weather" else 0)
        return f"результат {name}"

    monkeypatch.setattr(realtime_mod, "dispatch", fake_dispatch)

    await voice._run_tools([_call("c1", "get_weather"), _call("c2", "read_sensors")])

    assert [i["call_id"] for i in conn.created_items] == ["c1", "c2"]
    assert [i["output"] for i in conn.created_items] == [
        "результат get_weather", "результат read_sensors",
    ]
    assert conn.responses_created == 1


class _CancelRecorder:
    def __init__(self):
        self.cancelled = 0

    async def cancel(self):
        self.cancelled += 1


async def _noop_async(*args, **kwargs):
    return None


async def test_barge_in_cancels_only_an_active_response():
    """Отмена на каждое «Джарвис» без идущего ответа сыпала в лог
    «Cancellation failed: no active response found»."""
    voice = RealtimeVoice.__new__(RealtimeVoice)
    response = _CancelRecorder()
    voice._conn = types.SimpleNamespace(
        response=response,
        input_audio_buffer=types.SimpleNamespace(clear=_noop_async),
    )
    voice._cb = types.SimpleNamespace(drop_audio=_noop_async)

    await voice.barge_in()
    assert response.cancelled == 0

    await voice._on_event(types.SimpleNamespace(type="response.created"))
    await voice.barge_in()
    assert response.cancelled == 1
    assert voice._response_active is False


class _EndCallbacks:
    def __init__(self):
        self.turn_done_called = False
        self.end_called = False

    async def wait_drained(self):
        pass

    async def turn_done(self):
        self.turn_done_called = True

    async def end_conversation(self):
        self.end_called = True


def _voice_for_response_done():
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._cost = types.SimpleNamespace(add=lambda usage: None)
    voice._cb = _EndCallbacks()
    voice._transcript = ""
    voice._speaking = True
    voice._conn = _FakeConn()
    voice._flush_exchange = _noop_async
    return voice


def _done(*calls):
    return types.SimpleNamespace(
        type="response.done",
        response=types.SimpleNamespace(usage=None, output=list(calls)),
    )


async def test_end_conversation_closes_silently_without_followup():
    voice = _voice_for_response_done()

    await voice._on_event(_done(_call("c1", "end_conversation")))

    assert voice._conn.responses_created == 0, "после end_conversation говорить нечего"
    assert voice._cb.end_called and not voice._cb.turn_done_called
    assert voice._conn.created_items[0]["call_id"] == "c1"


async def test_end_conversation_mixed_with_other_tools_is_ignored(monkeypatch):
    voice = _voice_for_response_done()
    voice._ctx = None
    started = []

    async def fake_run_tools(calls):
        started.append([c.name for c in calls])

    voice._run_tools = fake_run_tools

    await voice._on_event(_done(_call("c1", "get_weather"), _call("c2", "end_conversation")))
    await asyncio.sleep(0)

    assert started == [["get_weather", "end_conversation"]]
    assert not voice._cb.end_called


# ---------- реанимация после исчерпания попыток ----------


async def test_reconnect_that_gave_up_keeps_trying_in_the_background(monkeypatch):
    """Раньше после трёх неудач _reconnect сдавался насовсем: _conn оставался
    None, плановое обновление при мёртвом соединении молча выходило, и голос
    не возвращался до ручного вмешательства (колонка при этом «Готова»)."""
    monkeypatch.setattr(realtime_mod, "_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(realtime_mod, "_RECONNECT_BACKOFF_S", (0.0,))
    monkeypatch.setattr(realtime_mod, "_REVIVE_INTERVAL_S", 0.01)
    voice = _idle_voice()
    voice._conn = None
    voice._ctx = types.SimpleNamespace(speak=_noop_speak)
    attempts = []

    async def recovers_on_fourth_try(history, summary=""):
        attempts.append(1)
        if len(attempts) < 4:
            raise RuntimeError("сети нет")
        voice._conn = object()

    voice.start = recovers_on_fourth_try

    await voice._reconnect()
    assert voice._revive_task is not None, "после сдачи должен стартовать реаниматор"
    await asyncio.wait_for(voice._revive_task, timeout=2)

    assert len(attempts) == 4
    assert voice._conn is not None


async def test_revive_loop_stops_when_the_session_is_closed(monkeypatch):
    monkeypatch.setattr(realtime_mod, "_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(realtime_mod, "_RECONNECT_BACKOFF_S", (0.0,))
    monkeypatch.setattr(realtime_mod, "_REVIVE_INTERVAL_S", 0.01)
    voice = _idle_voice()
    voice._conn = None
    voice._ctx = types.SimpleNamespace(speak=_noop_speak)
    attempts = []

    async def never_works(history, summary=""):
        attempts.append(1)
        raise RuntimeError("сети нет")

    voice.start = never_works

    await voice._reconnect()
    await asyncio.sleep(0.05)
    voice._closed = True  # так close() помечает конец сессии
    seen = len(attempts)
    await asyncio.wait_for(voice._revive_task, timeout=2)
    await asyncio.sleep(0.05)

    assert len(attempts) <= seen + 1, "после закрытия сессии попытки прекращаются"


async def test_start_revive_does_not_spawn_a_second_loop(monkeypatch):
    monkeypatch.setattr(realtime_mod, "_REVIVE_INTERVAL_S", 10.0)
    voice = _idle_voice()
    voice._conn = None

    voice._start_revive()
    first = voice._revive_task
    voice._start_revive()

    assert voice._revive_task is first
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first


async def _noop_speak(text):
    pass


# ---------- принудительный вызов инструмента на продолжении разговора ----------


class _ResponseRecorder:
    def __init__(self):
        self.created: list = []

    async def create(self, **kwargs):
        self.created.append(kwargs)


class _CommitBuffer:
    async def commit(self):
        pass


class _FullCallbacks(_Callbacks):
    async def drop_audio(self):
        pass

    async def set_state(self, state):
        pass


class _ItemRecorder:
    def __init__(self, log=None):
        self.created: list = []
        self._log = log

    async def create(self, **kwargs):
        self.created.append(kwargs["item"])
        if self._log is not None:
            self._log.append("item")


class _OrderedCommitBuffer:
    def __init__(self, log):
        self._log = log

    async def commit(self):
        self._log.append("commit")


def _voice_ready_to_end(expert_model="gpt-5.4-mini"):
    voice = _idle_voice()
    voice._mic_batch = bytearray()
    voice._transcript = ""
    voice._pending_user = None
    voice._user_ready = asyncio.Event()
    voice._flush_mic = _noop_flush
    voice._cb = _FullCallbacks()
    voice._settings = types.SimpleNamespace(expert_model=expert_model)
    order: list = []
    voice.order = order
    voice._conn = types.SimpleNamespace(
        input_audio_buffer=_OrderedCommitBuffer(order),
        response=_ResponseRecorder(),
        conversation=types.SimpleNamespace(item=_ItemRecorder(order)),
    )
    return voice


async def _noop_flush():
    pass


async def test_followup_utterance_is_marked_but_nothing_is_forced():
    """tool_choice=required на продолжении заставлял модель что-то вызвать
    даже на обрывке эха — и она снова включала музыку. Теперь только
    пометка перед репликой, модель вправе промолчать через end_conversation."""
    voice = _voice_ready_to_end()
    await voice.begin_utterance(followup=True)
    await voice.end_utterance()

    assert voice._conn.response.created == [{}]
    items = voice._conn.conversation.item.created
    assert len(items) == 1 and items[0]["role"] == "system"
    assert "end_conversation" in items[0]["content"][0]["text"]
    assert voice.order == ["item", "commit"], "пометка должна идти ДО реплики"


async def test_regular_utterance_has_no_followup_note():
    voice = _voice_ready_to_end()
    await voice.begin_utterance(followup=False)
    await voice.end_utterance()

    assert voice._conn.response.created == [{}]
    assert voice._conn.conversation.item.created == []


async def test_followup_flag_does_not_leak_into_the_next_utterance():
    voice = _voice_ready_to_end()
    await voice.begin_utterance(followup=True)
    await voice.begin_utterance(followup=False)
    await voice.end_utterance()

    assert voice._conn.response.created == [{}]
    assert voice._conn.conversation.item.created == []
