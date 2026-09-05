"""Конечный автомат сессии — самый нагруженный код без единого теста.

Полный Session() требует настоящих клиентов Anthropic/OpenAI (voice_provider
конструирует RealtimeVoice/ClaudeVoice в __init__) и реальной сети для
_start_voice(). Вместо этого — bare-instance тесты, как уже сделано для
RealtimeVoice в test_realtime.py: собираем голый объект и подставляем
только то, что трогает проверяемый путь.
"""

import asyncio
import types

import app.session as session_mod
from app.peers import ROLE_SATELLITE, Peer
from app.protocol import State
from app.session import Session


class _FakeVoice:
    def __init__(self):
        self.barge_in_called = False
        self.end_utterance_called = False

    async def barge_in(self):
        self.barge_in_called = True

    async def end_utterance(self):
        self.end_utterance_called = True


class _FakeWS:
    def __init__(self):
        self.sent_json = []

    async def send_json(self, data):
        self.sent_json.append(data)


class _FakeVAD:
    def __init__(self, heard_speech):
        self.heard_speech = heard_speech


def _idle_session(heard_speech: bool, mic_bytes: int) -> Session:
    session = Session.__new__(Session)
    session._settings = types.SimpleNamespace(mic_sample_rate=16_000)
    session._vad = _FakeVAD(heard_speech=heard_speech)
    session._recording = True
    session._mic_bytes = mic_bytes
    session._voice = _FakeVoice()
    session._mixer = types.SimpleNamespace(is_playing=False, set_listening=lambda v: None)
    # Устройств в сессии может быть несколько; здесь проверяется путь
    # с одним, поэтому набор пиров — заглушка.
    session._peers = types.SimpleNamespace(
        release_active=lambda: None, all=lambda: [], screens=lambda: []
    )
    session._state = State.LISTENING
    session._state_watchdog = None
    session._screen = None
    session._screen_text = ""
    return session


async def test_empty_reply_returns_to_idle_without_reaching_voice():
    """Пустая реплика (тишина или обрывок ложного пробуждения) не должна
    уходить в голосовой бэкенд — иначе модель отвечает по случайному шуму.
    """
    session = _idle_session(heard_speech=False, mic_bytes=0)

    await session._stop_recording()

    assert session._voice.barge_in_called
    assert not session._voice.end_utterance_called
    assert session._recording is False
    assert session._state == State.IDLE


async def test_short_speech_below_minimum_also_stays_local():
    """Речь была, но короче минимума (600 мс) — тот же путь, что и тишина."""
    too_short = 16_000 * 400 // 1000 * 2  # 400 мс, порог — 600 мс
    session = _idle_session(heard_speech=True, mic_bytes=too_short)

    await session._stop_recording()

    assert session._voice.barge_in_called
    assert not session._voice.end_utterance_called


async def test_real_speech_reaches_the_voice_backend():
    """Достаточно долгая услышанная речь обязана уйти в голосовой бэкенд."""
    enough = 16_000 * 1000 // 1000 * 2  # секунда, с запасом выше минимума
    session = _idle_session(heard_speech=True, mic_bytes=enough)

    await session._stop_recording()

    assert session._voice.end_utterance_called
    assert not session._voice.barge_in_called


async def test_stop_recording_is_a_noop_when_not_recording():
    """Повторный вызов (гонка событий) не должен трогать голосовой бэкенд."""
    session = _idle_session(heard_speech=True, mic_bytes=100_000)
    session._recording = False

    await session._stop_recording()

    assert not session._voice.barge_in_called
    assert not session._voice.end_utterance_called


async def test_watchdog_forces_idle_when_state_never_advances(monkeypatch):
    """Сторож незавершённых состояний: если LISTENING/THINKING/SPEAKING не
    сменилось само за отведённое время, сессия обязана вернуться в IDLE.

    Наблюдали дважды разными путями — Realtime не прислал response.done
    (застряла в THINKING) и реплика оборвалась ровно в момент планового
    переподключения раз в час (застряла в LISTENING на часы, пока не
    вмешались руками). Это последняя линия защиты от обоих случаев и любых
    похожих, ещё не встреченных."""
    monkeypatch.setattr(session_mod, "_STATE_WATCHDOG_S", 0.01)

    session = Session.__new__(Session)
    session._state = State.IDLE
    session._screen_text = ""
    session._screen = None
    session._client_has_screen = False
    session._recording = True
    session._state_watchdog = None
    session._voice = _FakeVoice()
    session._mixer = types.SimpleNamespace(is_playing=False)
    session._peers = types.SimpleNamespace(
        all=lambda: [], screens=lambda: [], release_active=lambda: None,
    )

    await session._set_state(State.LISTENING)
    await asyncio.sleep(0.05)

    assert session._state == State.IDLE
    assert session._recording is False
    assert session._voice.barge_in_called


async def test_watchdog_does_not_fire_once_state_moves_on(monkeypatch):
    """Обычный, не зависший разговор не должен трогаться сторожем."""
    monkeypatch.setattr(session_mod, "_STATE_WATCHDOG_S", 0.05)

    session = Session.__new__(Session)
    session._state = State.IDLE
    session._screen_text = ""
    session._screen = None
    session._client_has_screen = False
    session._recording = True
    session._state_watchdog = None
    session._voice = _FakeVoice()
    session._mixer = types.SimpleNamespace(is_playing=False)
    session._peers = types.SimpleNamespace(
        all=lambda: [], screens=lambda: [], release_active=lambda: None,
    )

    await session._set_state(State.LISTENING)
    await session._set_state(State.THINKING)
    await session._set_state(State.SPEAKING)
    await session._set_state(State.IDLE)
    await asyncio.sleep(0.08)

    assert session._state == State.IDLE
    assert not session._voice.barge_in_called, "сторож не должен был сработать вовсе"


async def test_satellite_hello_gets_current_volume():
    """Сателлит не играет звук сам, но регулирует громкость physической
    колонки с телефона — без этого сообщения ползунок на странице не
    знает, с какого значения начинать."""
    session = Session.__new__(Session)
    session._settings = types.SimpleNamespace(mic_sample_rate=16_000, frame_samples_mic=320)
    session._mixer = types.SimpleNamespace(volume=0.42)
    session._state = State.IDLE

    ws = _FakeWS()
    peer = Peer(ws, device="phone", role=ROLE_SATELLITE, has_screen=False)

    await session._on_hello(peer, {"codec": "pcm"})

    kinds = [m["t"] for m in ws.sent_json]
    assert "volume" in kinds
    volume_msg = next(m for m in ws.sent_json if m["t"] == "volume")
    assert volume_msg["value"] == 0.42
