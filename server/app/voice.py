"""Голосовой бэкенд сессии: контракт и путь по умолчанию.

`Session` не знает, что именно отвечает на реплику — она умеет только
кормить бэкенд аудио с микрофона и просить начать/закончить реплику.
Бэкенд сам решает, гонять ли звук через STT/LLM/TTS или потоком в
спич-ту-спич модель, и отчитывается о своих действиях через колбэки.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.agent import Agent
from app.config import Settings
from app.memory import Turn
from app.protocol import State
from app.stt import SpeechToText
from app.tools.context import ToolContext
from app.tts import TextToSpeech

log = logging.getLogger(__name__)


@dataclass
class VoiceCallbacks:
    set_state: Callable[[State], Awaitable[None]]
    show_text: Callable[[str], Awaitable[None]]
    push_audio: Callable[[bytes], Awaitable[None]]
    drop_audio: Callable[[], Awaitable[None]]
    wait_drained: Callable[[], Awaitable[None]]
    # Реплика полностью отзвучала — сессия решает, в какое состояние встать.
    turn_done: Callable[[], Awaitable[None]]
    # Реплика (своя или ассистента) целиком готова — сохранить на диск.
    save_turn: Callable[[str, str], Awaitable[None]]


class VoiceBackend(Protocol):
    async def start(self, history: list[Turn], summary: str = "") -> None: ...
    async def begin_utterance(self) -> None: ...
    async def feed(self, pcm: bytes) -> None: ...
    async def end_utterance(self) -> None: ...
    async def barge_in(self) -> None: ...
    async def close(self) -> None: ...


class ClaudeVoice:
    """STT (Whisper) → Claude с инструментами → TTS (Piper). Путь по умолчанию."""

    def __init__(
        self,
        settings: Settings,
        ctx: ToolContext,
        stt: SpeechToText,
        tts: TextToSpeech,
        cb: VoiceCallbacks,
    ):
        self._stt = stt
        self._tts = tts
        self._agent = Agent(settings, ctx)
        self._cb = cb
        self._buffer = bytearray()
        self._turn: asyncio.Task | None = None

    async def start(self, history: list[Turn], summary: str = "") -> None:
        self._agent.seed_history(history, summary)

    async def begin_utterance(self) -> None:
        await self.barge_in()
        self._buffer.clear()

    async def feed(self, pcm: bytes) -> None:
        self._buffer.extend(pcm)

    async def end_utterance(self) -> None:
        pcm = bytes(self._buffer)
        self._buffer.clear()
        self._turn = asyncio.create_task(self._run(pcm))

    async def barge_in(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
        await self._cb.drop_audio()

    async def close(self) -> None:
        if self._turn is not None:
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn

    async def _run(self, pcm: bytes) -> None:
        try:
            await self._cb.set_state(State.THINKING)
            text = await self._stt.transcribe(pcm)
            if not text:
                await self._cb.turn_done()
                return

            log.info("пользователь: %s", text)
            await self._cb.show_text(text)
            await self._cb.save_turn("user", text)
            await self._cb.set_state(State.SPEAKING)

            reply_parts: list[str] = []

            async def on_sentence(sentence: str) -> None:
                reply_parts.append(sentence)
                await self._speak_sentence(sentence)

            await self._agent.respond(text, on_sentence)
            if reply_parts:
                await self._cb.save_turn("assistant", " ".join(reply_parts))
            await self._cb.wait_drained()
            await self._cb.turn_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("реплика не обработана")
            await self._speak_sentence("Что-то пошло не так, попробуй ещё раз.")
            await self._cb.wait_drained()
            await self._cb.turn_done()

    async def _speak_sentence(self, sentence: str) -> None:
        log.info("ассистент: %s", sentence)
        await self._cb.show_text(sentence)
        # Потоком: облачный синтез отдаёт первые сэмплы задолго до конца
        # фразы, и колонка начинает говорить, не дожидаясь её целиком.
        await self._tts.synthesize_stream(sentence, self._cb.push_audio)
