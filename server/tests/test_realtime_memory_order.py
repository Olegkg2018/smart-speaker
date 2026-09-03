"""В память обмен должен ложиться как «вопрос, потом ответ».

Realtime отвечает прямо на звук, а расшифровку вопроса Whisper досылает
параллельно — событие `input_audio_transcription.completed` регулярно
приходит уже после `response.done`. Записывая по факту прихода, сервер
клал ответ раньше вопроса: в памяти получались пары `assistant` подряд и
диалог со сдвигом на ход, из-за чего в следующем разговоре модель
отвечала на предыдущий вопрос.
"""

import asyncio
import types

from app.realtime import RealtimeVoice


class _RecordingCallbacks:
    def __init__(self):
        self.turns: list[tuple[str, str]] = []

    async def save_turn(self, role, text):
        self.turns.append((role, text))

    async def show_text(self, text):
        return None

    async def wait_drained(self):
        return None

    async def turn_done(self):
        return None


def _voice() -> RealtimeVoice:
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._cb = _RecordingCallbacks()
    voice._pending_user = None
    voice._pending_assistant = None
    voice._user_ready = asyncio.Event()
    voice._transcript = ""
    voice._speaking = False
    voice._cost = types.SimpleNamespace(add=lambda _usage: None)
    return voice


def _user_transcript(text):
    return types.SimpleNamespace(
        type="conversation.item.input_audio_transcription.completed", transcript=text
    )


def _response_done(text):
    """response.done без вызовов инструментов."""
    return types.SimpleNamespace(
        type="response.done",
        response=types.SimpleNamespace(output=[], usage=None),
    )


async def test_answer_arriving_before_question_is_still_stored_in_order():
    """Главный случай: ответ готов раньше расшифровки вопроса."""
    voice = _voice()

    voice._transcript = "Взбей яйца с сахаром."
    await voice._on_event(_response_done(voice._transcript))

    # Расшифровка вопроса приходит уже после ответа.
    await voice._on_event(_user_transcript("Как приготовить шарлотку?"))
    await asyncio.sleep(0.05)  # даём отработать фоновой записи

    assert voice._cb.turns == [
        ("user", "Как приготовить шарлотку?"),
        ("assistant", "Взбей яйца с сахаром."),
    ]


async def test_question_arriving_first_keeps_order():
    """Обычный порядок тоже должен сохраняться."""
    voice = _voice()

    await voice._on_event(_user_transcript("Который час?"))
    voice._transcript = "Половина шестого."
    await voice._on_event(_response_done(voice._transcript))
    await asyncio.sleep(0.05)

    assert voice._cb.turns == [
        ("user", "Который час?"),
        ("assistant", "Половина шестого."),
    ]


async def test_lost_transcript_does_not_lose_the_answer():
    """Расшифровка не пришла вовсе — ответ всё равно попадает в память."""
    voice = _voice()

    import app.realtime as realtime_mod

    original = realtime_mod._TRANSCRIPT_WAIT_S
    realtime_mod._TRANSCRIPT_WAIT_S = 0.05
    try:
        voice._transcript = "Готово."
        await voice._on_event(_response_done(voice._transcript))
        await asyncio.sleep(0.2)
    finally:
        realtime_mod._TRANSCRIPT_WAIT_S = original

    assert voice._cb.turns == [("assistant", "Готово.")]


async def test_new_utterance_does_not_inherit_previous_question():
    """Хвост прошлого обмена не должен приклеиваться к новой реплике."""
    voice = _voice()
    voice._pending_user = "старый вопрос"
    voice._user_ready.set()

    async def _noop():
        return None

    voice.barge_in = _noop
    voice._mic_batch = bytearray()

    await voice.begin_utterance()

    assert voice._pending_user is None
    assert not voice._user_ready.is_set()
