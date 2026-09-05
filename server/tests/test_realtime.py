"""Приём событий от облака не должен глушить отправку звука на колонку."""

import asyncio
import types

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


class _HangingManager:
    """Как настоящий менеджер соединения, но __aenter__ никогда не отвечает.

    Живой случай: сетевая заминка подвесила именно этот вызов на 14+ минут
    без единого исключения — start() ждал вечно, self._conn оставался None
    и это никого не удивляло: ошибки-то не было."""

    def __init__(self):
        self.aexit_called = False

    async def __aenter__(self):
        await asyncio.sleep(999)

    async def __aexit__(self, *exc):
        self.aexit_called = True


async def test_start_gives_up_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr(realtime_mod, "_CONNECT_TIMEOUT_S", 0.01)
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._refresh_task = None
    manager = _HangingManager()
    voice._client = types.SimpleNamespace(
        realtime=types.SimpleNamespace(connect=lambda model: manager)
    )
    voice._settings = types.SimpleNamespace(openai_realtime_model="gpt-realtime")

    raised = False
    try:
        await voice.start([], "")
    except TimeoutError:
        raised = True

    assert raised, "зависшее подключение должно превращаться в быструю ошибку"
    assert voice._conn is None
    assert voice._manager is None
    assert manager.aexit_called, "частично открытое соединение нужно закрыть, а не бросить"


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

    await voice._run_tool("call-1", "web_search", "{}")

    assert old_conn.created_items == [] and old_conn.responses_created == 0
    assert new_conn.created_items == [] and new_conn.responses_created == 0
