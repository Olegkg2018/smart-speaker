"""Конечный автомат сессии — самый нагруженный код без единого теста.

Полный Session() требует настоящих клиентов Anthropic/OpenAI (voice_provider
конструирует RealtimeVoice/ClaudeVoice в __init__) и реальной сети для
_start_voice(). Вместо этого — bare-instance тесты, как уже сделано для
RealtimeVoice в test_realtime.py: собираем голый объект и подставляем
только то, что трогает проверяемый путь.
"""

import asyncio
import time
import types

import numpy as np

import app.session as session_mod
from app.audio.speaker_level import SpeakerLevel
from app.audio.vad import SilenceDetector
from app.peers import ROLE_SATELLITE, ROLE_SPEAKER, Peer, PeerSet
from app.protocol import FRAME_MIC, State, pack_audio
from app.session import Session


class _FakeVoice:
    def __init__(self):
        self.barge_in_called = False
        self.end_utterance_called = False
        self.begin_utterance_called = False
        self.fed: list[bytes] = []

    async def barge_in(self):
        self.barge_in_called = True

    async def end_utterance(self):
        self.end_utterance_called = True

    async def begin_utterance(self):
        self.begin_utterance_called = True

    async def feed(self, pcm):
        self.fed.append(pcm)


class _FakeWS:
    def __init__(self):
        self.sent_json = []

    async def send_json(self, data):
        self.sent_json.append(data)


class _FakeVAD:
    def __init__(self, heard_speech):
        self.heard_speech = heard_speech
        self.reset_called = False

    def reset(self):
        self.reset_called = True


class _FakeSpeakerLevel:
    """Заглушка для тестов, которым не важна сама эвристика громкости."""

    def __init__(self, accept: bool = True):
        self.reset_called = False
        self.capture_called = False
        self._accept = accept

    def reset(self):
        self.reset_called = True

    def observe(self, raw_level, is_speech):
        pass

    def capture_reference(self):
        self.capture_called = True

    def accepts(self, raw_level):
        return self._accept


class _PassthroughCodec:
    """Кодек-заглушка: PCM как есть, без сжатия — для тестов на _on_binary."""

    name = "pcm"

    def decode(self, payload: bytes) -> bytes:
        return payload

    def encode(self, pcm: bytes) -> bytes:
        return pcm


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
        release_active=lambda: None, all=lambda: [], screens=lambda: [], active=None
    )
    session._state = State.LISTENING
    session._state_watchdog = None
    session._screen = None
    session._screen_text = ""
    session._in_followup = False
    session._followup_watchdog = None
    session._speaker_level = _FakeSpeakerLevel()
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
    session._in_followup = True  # сторож должен закрыть и это заодно
    session._followup_watchdog = None
    session._voice = _FakeVoice()
    session._mixer = types.SimpleNamespace(is_playing=False)
    session._peers = types.SimpleNamespace(
        all=lambda: [], screens=lambda: [], release_active=lambda: None,
    )

    await session._set_state(State.LISTENING)
    await asyncio.sleep(0.05)

    assert session._state == State.IDLE
    assert session._recording is False
    assert session._in_followup is False
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
    session._in_followup = False
    session._followup_watchdog = None
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
    session._settings = types.SimpleNamespace(
        mic_sample_rate=16_000, frame_samples_mic=320, followup_enabled=False
    )
    session._mixer = types.SimpleNamespace(volume=0.42)
    session._state = State.IDLE

    ws = _FakeWS()
    peer = Peer(ws, device="phone", role=ROLE_SATELLITE, has_screen=False)

    await session._on_hello(peer, {"codec": "pcm"})

    kinds = [m["t"] for m in ws.sent_json]
    assert "volume" in kinds
    volume_msg = next(m for m in ws.sent_json if m["t"] == "volume")
    assert volume_msg["value"] == 0.42


# ---------- продолжение разговора без нового активационного слова ----------


def _finish_turn_session(*, followup_enabled: bool, is_playing: bool, peer: Peer | None):
    session = Session.__new__(Session)
    session._settings = types.SimpleNamespace(followup_enabled=followup_enabled)
    session._mixer = types.SimpleNamespace(is_playing=is_playing)
    session._last_active_peer = peer
    session._peers = types.SimpleNamespace(all=lambda: [peer] if peer is not None else [])
    calls: list = []

    async def fake_idle():
        calls.append("idle")

    async def fake_followup(p):
        calls.append(("followup", p))

    session._set_idle = fake_idle
    session._start_followup_window = fake_followup
    return session, calls


async def test_finish_turn_goes_idle_when_followup_disabled():
    peer = Peer(ws=None, device="kitchen", role=ROLE_SPEAKER, has_screen=False)
    peer.note_raw_level(500.0)
    session, calls = _finish_turn_session(followup_enabled=False, is_playing=False, peer=peer)

    await session._finish_turn()

    assert calls == ["idle"]


async def test_finish_turn_goes_idle_while_music_is_playing():
    peer = Peer(ws=None, device="kitchen", role=ROLE_SPEAKER, has_screen=False)
    peer.note_raw_level(500.0)
    session, calls = _finish_turn_session(followup_enabled=True, is_playing=True, peer=peer)

    await session._finish_turn()

    assert calls == ["idle"]


async def test_finish_turn_goes_idle_without_raw_level_reference():
    """Устройство ещё ни разу не прислало mic_level (старая прошивка или
    первая же реплика в сессии) — фича не должна активироваться вслепую."""
    peer = Peer(ws=None, device="kitchen", role=ROLE_SPEAKER, has_screen=False)
    session, calls = _finish_turn_session(followup_enabled=True, is_playing=False, peer=peer)

    await session._finish_turn()

    assert calls == ["idle"]


async def test_finish_turn_goes_idle_without_a_last_peer():
    session, calls = _finish_turn_session(followup_enabled=True, is_playing=False, peer=None)

    await session._finish_turn()

    assert calls == ["idle"]


async def test_finish_turn_opens_followup_window_when_eligible():
    peer = Peer(ws=None, device="kitchen", role=ROLE_SPEAKER, has_screen=False)
    peer.note_raw_level(500.0)
    session, calls = _finish_turn_session(followup_enabled=True, is_playing=False, peer=peer)

    await session._finish_turn()

    assert calls == [("followup", peer)]


async def test_start_followup_window_opens_mic_without_new_wake_word():
    """_set_state(LISTENING) — это и есть команда прошивке открыть
    микрофон (см. ws_client.c::handle_text, ветка "state") — новый
    исходящий тип сообщения не нужен."""
    session = Session.__new__(Session)
    peers = PeerSet()
    peer = Peer(ws=None, device="kitchen", role=ROLE_SPEAKER, has_screen=False)
    peers.add(peer)
    session._peers = peers
    session._voice = _FakeVoice()
    session._vad = _FakeVAD(heard_speech=False)
    session._speaker_level = _FakeSpeakerLevel()
    session._mixer = types.SimpleNamespace(set_listening=lambda v: None)
    session._mic_bytes = 999
    session._listen_started = 0.0
    session._recording = False
    session._in_followup = False
    session._followup_watchdog = None
    session._settings = types.SimpleNamespace(followup_window_s=4.0)
    set_state_calls: list = []

    async def fake_set_state(state):
        set_state_calls.append(state)

    session._set_state = fake_set_state

    await session._start_followup_window(peer)

    assert session._recording is True
    assert session._in_followup is True
    assert session._mic_bytes == 0
    assert session._vad.reset_called
    assert session._speaker_level.reset_called
    assert session._voice.begin_utterance_called
    assert session._peers.active is peer
    assert set_state_calls == [State.LISTENING]
    assert session._followup_watchdog is not None
    session._followup_watchdog.cancel()  # не оставляем висящую задачу теста


async def test_watch_followup_forces_cancel_after_timeout(monkeypatch):
    monkeypatch.setattr(session_mod, "_FOLLOWUP_WATCHDOG_MARGIN_S", 0.0)
    session = Session.__new__(Session)
    session._settings = types.SimpleNamespace(followup_window_s=0.01)
    session._in_followup = True
    # Что угодно, лишь бы не None и без .cancel() — если бы код по ошибке
    # звал self._clear_followup_watchdog() вместо прямого присваивания
    # None, это была бы самоотмена текущей задачи (тот же баг, что был с
    # self._refresh_task.cancel() на себе же в realtime.py, см. CLAUDE.md).
    session._followup_watchdog = object()
    cancel_calls: list = []

    async def fake_cancel():
        cancel_calls.append(1)

    session._cancel_recording = fake_cancel

    await session._watch_followup()

    assert session._in_followup is False
    assert session._followup_watchdog is None
    assert cancel_calls == [1]


async def test_watch_followup_is_noop_once_window_already_closed(monkeypatch):
    monkeypatch.setattr(session_mod, "_FOLLOWUP_WATCHDOG_MARGIN_S", 0.0)
    session = Session.__new__(Session)
    session._settings = types.SimpleNamespace(followup_window_s=0.01)
    session._in_followup = False
    cancel_calls: list = []

    async def fake_cancel():
        cancel_calls.append(1)

    session._cancel_recording = fake_cancel

    await session._watch_followup()

    assert cancel_calls == []


def _gate_session(min_ratio: float = 0.5):
    session = Session.__new__(Session)
    session._settings = types.SimpleNamespace(
        mic_sample_rate=16_000,
        wake_listen_timeout_s=8.0,
        followup_window_s=4.0,
    )
    peers = PeerSet()
    peer = Peer(ws=None, device="kitchen", role=ROLE_SPEAKER, has_screen=False)
    peer.codec = _PassthroughCodec()
    peers.add(peer)
    peers.keep_active(peer)  # как после _start_followup_window
    session._peers = peers
    session._mic_codec = _PassthroughCodec()
    session._mixer = types.SimpleNamespace(is_playing=False, set_listening=lambda v: None)
    session._vad = SilenceDetector(
        sample_rate=16_000, threshold=150, silence_ms=900, min_speech_ms=200
    )
    session._speaker_level = SpeakerLevel(min_ratio=min_ratio)
    session._voice = _FakeVoice()
    session._recording = True
    session._in_followup = True
    session._followup_watchdog = None
    session._mic_bytes = 0
    session._listen_started = time.monotonic()
    return session, peer


def _mic_frame(level: int, samples: int = 320) -> bytes:
    pcm = np.full(samples, level, dtype=np.int16).tobytes()
    return pack_audio(FRAME_MIC, pcm)


async def test_followup_gate_rejects_frame_far_quieter_than_reference():
    session, peer = _gate_session()
    session._speaker_level.observe(2000.0, True)
    session._speaker_level.capture_reference()
    peer.note_raw_level(200.0)  # заметно тише эталона (2000 * 0.5 = 1000)

    await session._on_binary(peer, _mic_frame(2000))

    assert session._in_followup is True, "чужой/тихий источник не снимает проверку"
    assert session._voice.fed == [], "отклонённый кадр не должен доходить до голоса"


async def test_followup_gate_accepts_matching_frame_and_opens_up():
    session, peer = _gate_session()
    session._speaker_level.observe(2000.0, True)
    session._speaker_level.capture_reference()
    peer.note_raw_level(1800.0)  # тот же порядок громкости — тот же человек

    await session._on_binary(peer, _mic_frame(2000))

    assert session._in_followup is False, "прошедший проверку кадр снимает режим окна"
    assert session._voice.fed, "принятый кадр обязан дойти до голосового бэкенда"


async def test_followup_gate_is_noop_without_raw_level():
    """Устройство ещё не прислало mic_level для этого кадра — не мешаем:
    фича должна быть строго аддитивной."""
    session, peer = _gate_session()
    session._speaker_level.observe(2000.0, True)
    session._speaker_level.capture_reference()
    # peer.raw_level остаётся None.

    await session._on_binary(peer, _mic_frame(2000))

    assert session._in_followup is False
    assert session._voice.fed
