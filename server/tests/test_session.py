"""Конечный автомат сессии — самый нагруженный код без единого теста.

Полный Session() требует настоящих клиентов Anthropic/OpenAI (voice_provider
конструирует RealtimeVoice/ClaudeVoice в __init__) и реальной сети для
_start_voice(). Вместо этого — bare-instance тесты, как уже сделано для
RealtimeVoice в test_realtime.py: собираем голый объект и подставляем
только то, что трогает проверяемый путь.
"""

import types

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
    session._mixer = types.SimpleNamespace(is_playing=False)
    session._ws = _FakeWS()
    session._state = State.LISTENING
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
