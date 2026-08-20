"""Голосовой бэкенд поверх OpenAI Realtime API: спич-ту-спич без STT и TTS.

Существует по одной причине: Whisper на S905X3 не разгоняется быстрее пяти
секунд на реплику (`tools/bench.py`, RTF 1.78) — окно у него всегда
тридцатисекундное независимо от длины фразы. Realtime вместо текста гоняет
аудио потоком в обе стороны, и эта стадия просто исчезает.

Кнопка на колонке — единственный источник истины о начале и конце реплики,
поэтому серверный VAD выключен (`turn_detection: None`): модель слушает
и отвечает по команде, а не сама решает, когда пользователь замолчал.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging

from openai import AsyncOpenAI

from app.agent import SYSTEM_PROMPT
from app.audio.resample import resample_pcm16
from app.config import Settings
from app.memory import Turn
from app.tools import notes as notes_tool
from app.pricing import CostMeter
from app.protocol import State
from app.tools.context import ToolContext
from app.tools.registry import TOOL_SCHEMAS, dispatch
from app.voice import VoiceCallbacks

log = logging.getLogger(__name__)

# API принимает и отдаёт PCM16 только на этой частоте — не настраивается.
_REALTIME_RATE = 24_000

def _decode_and_resample(delta: str, src_rate: int, dst_rate: int) -> bytes:
    return resample_pcm16(base64.b64decode(delta), src_rate, dst_rate)


def _build_instructions(history: list[Turn], notes: str = "") -> str:
    """SYSTEM_PROMPT плюс краткий пересказ прошлого разговора, если он есть.

    Realtime API не даёт напрямую подсадить историю сообщений в сессию так
    же чисто, как в Chat Completions — конкретный формат текстовых элементов
    conversation.item для сообщений не задокументирован достаточно точно,
    чтобы полагаться на него. Пересказ в инструкциях надёжнее и не зависит
    от точной схемы API.
    """
    if not history:
        return SYSTEM_PROMPT + notes
    recap = "\n".join(
        f"{'Пользователь' if t.role == 'user' else 'Ты'}: {t.text}" for t in history
    )
    return (
        f"{SYSTEM_PROMPT}{notes}\n\n"
        "Ниже — последние реплики более раннего разговора с этим человеком, "
        "для контекста. Это не текущая реплика, отвечать на неё не нужно:\n"
        f"{recap}"
    )


_TOOLS = [
    {
        "type": "function",
        "name": t["name"],
        "description": t["description"],
        "parameters": t["input_schema"],
    }
    for t in TOOL_SCHEMAS
]


class RealtimeVoice:
    def __init__(self, settings: Settings, ctx: ToolContext, cb: VoiceCallbacks):
        self._settings = settings
        self._ctx = ctx
        self._cb = cb
        self._client = AsyncOpenAI(api_key=settings.openai_api_key or None)
        self._manager = None
        self._conn = None
        self._recv_task: asyncio.Task | None = None
        # Аудио ответа приходит раньше, чем мы успеваем отреагировать на первый
        # байт — по этому флагу переключаем состояние ровно один раз за реплику.
        self._speaking = False
        self._transcript = ""
        self._cost = CostMeter(settings.openai_realtime_model)

    async def start(self, history: list[Turn]) -> None:
        self._manager = self._client.realtime.connect(model=self._settings.openai_realtime_model)
        self._conn = await self._manager.__aenter__()
        await self._conn.session.update(
            session={
                "type": "realtime",
                "instructions": _build_instructions(history, notes_tool.as_instructions(self._settings.notes_dir)),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": _REALTIME_RATE},
                        "turn_detection": None,
                        "transcription": {"model": "whisper-1"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": _REALTIME_RATE},
                        "voice": self._settings.openai_voice,
                    },
                },
                "tools": _TOOLS,
                # Без этого контекст растёт неограниченно, а Realtime считает
                # входные токены за весь накопленный разговор на каждый ответ —
                # десятая реплика стоит как десять первых.
                "truncation": {
                    "type": "retention_ratio",
                    "retention_ratio": self._settings.realtime_retention_ratio,
                    "token_limits": {
                        "post_instructions": self._settings.realtime_context_tokens
                    },
                },
            }
        )
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def begin_utterance(self) -> None:
        await self.barge_in()
        self._transcript = ""

    async def feed(self, pcm: bytes) -> None:
        if self._conn is None:
            return
        # Кадр микрофона — всего 20 мс, пересчёт занимает десятки микросекунд.
        # Уводить такую мелочь в поток дороже, чем посчитать на месте.
        pcm24 = resample_pcm16(pcm, self._settings.mic_sample_rate, _REALTIME_RATE)
        await self._conn.input_audio_buffer.append(audio=base64.b64encode(pcm24).decode("ascii"))

    async def end_utterance(self) -> None:
        if self._conn is None:
            return
        await self._cb.set_state(State.THINKING)
        await self._conn.input_audio_buffer.commit()
        await self._conn.response.create()

    async def barge_in(self) -> None:
        if self._conn is not None:
            # Отменять можно и когда отвечать нечему — сервер просто откажет.
            with contextlib.suppress(Exception):
                await self._conn.response.cancel()
            with contextlib.suppress(Exception):
                await self._conn.input_audio_buffer.clear()
        self._speaking = False
        await self._cb.drop_audio()

    async def close(self) -> None:
        if self._cost.turns:
            log.info(
                "разговор окончен: %.4f $ за %d реплик (в среднем %.4f $ на реплику)",
                self._cost.total_usd,
                self._cost.turns,
                self._cost.total_usd / self._cost.turns,
            )
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
        if self._manager is not None:
            with contextlib.suppress(Exception):
                await self._manager.__aexit__(None, None, None)

    async def _recv_loop(self) -> None:
        assert self._conn is not None
        try:
            async for event in self._conn:
                try:
                    await self._on_event(event)
                except Exception:
                    log.exception("сбой обработки события realtime")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Соединение оборвалось (неверный ключ, сеть, квота) — исчерпав
            # встроенные ретраи SDK, оно просто закрывается. Не даём сессии
            # зависнуть в THINKING/SPEAKING навсегда: возвращаемся в IDLE и
            # считаем соединение мёртвым, чтобы feed()/end_utterance() не
            # пытались писать в закрытый сокет.
            log.error("соединение с OpenAI Realtime оборвалось: %s", exc)
            self._conn = None
            self._speaking = False
            with contextlib.suppress(Exception):
                await self._cb.wait_drained()
            await self._cb.turn_done()

    async def _on_event(self, event) -> None:
        etype = event.type
        if etype == "response.output_audio.delta":
            if not self._speaking:
                self._speaking = True
                await self._cb.set_state(State.SPEAKING)
            # Декодирование и пересчёт частоты — чистый расчёт на numpy, и на
            # четырёх Cortex-A55 он занимает заметное время. В цикле событий
            # это останавливает отправку кадров колонке, и речь идёт рывками,
            # поэтому считаем в отдельном потоке.
            pcm48 = await asyncio.to_thread(
                _decode_and_resample,
                event.delta,
                _REALTIME_RATE,
                self._settings.out_sample_rate,
            )
            await self._cb.push_audio(pcm48)
        elif etype == "response.output_audio_transcript.delta":
            self._transcript += event.delta
            await self._cb.show_text(self._transcript)
        elif etype == "conversation.item.input_audio_transcription.completed":
            await self._cb.show_text(event.transcript)
            await self._cb.save_turn("user", event.transcript)
        elif etype == "response.done":
            self._cost.add(getattr(event.response, "usage", None))
            # Ответ, целиком состоящий из вызова инструмента, не значит, что
            # реплика закончилась — следом придёт ещё один response с озвучкой
            # результата, и вот на нём уже нужно гасить состояние.
            calls = [
                item
                for item in event.response.output
                if getattr(item, "type", None) == "function_call"
            ]
            if calls:
                for item in calls:
                    asyncio.create_task(self._run_tool(item.call_id, item.name, item.arguments))
                return
            self._speaking = False
            if self._transcript:
                await self._cb.save_turn("assistant", self._transcript)
            self._transcript = ""
            await self._cb.wait_drained()
            await self._cb.turn_done()
        elif etype == "error":
            log.warning("realtime сообщил об ошибке: %s", event.error)

    async def _run_tool(self, call_id: str, name: str, raw_args: str) -> None:
        log.info("инструмент %s(%s)", name, raw_args)
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
        output = await dispatch(self._ctx, name, args)
        if self._conn is None:
            return
        await self._conn.conversation.item.create(
            item={"type": "function_call_output", "call_id": call_id, "output": output}
        )
        await self._conn.response.create()
