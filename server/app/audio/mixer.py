"""Микшер исходящего звука.

Единственный источник аудио для колонки. Складывает музыку и речь ассистента
в один непрерывный поток 48 кГц mono, приглушая музыку на время реплики
(ducking). Благодаря этому прошивка ESP32 остаётся тривиальной: она получает
готовые кадры и просто отдаёт их в I2S.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import numpy as np

# За сколько миллисекунд громкость музыки доезжает до нового уровня.
# Мгновенное переключение даёт слышимый щелчок.
_RAMP_MS = 150


class AudioSource(Protocol):
    """Источник PCM: музыка, поток радио, файл."""

    async def read(self, n_samples: int) -> np.ndarray | None:
        """Возвращает ровно `n_samples` int16 или None, если источник кончился."""
        ...

    async def close(self) -> None: ...


class AudioMixer:
    def __init__(
        self,
        frame_samples: int,
        frame_ms: int,
        duck_level: float = 0.2,
        volume: float = 0.7,
    ):
        self.frame_samples = frame_samples
        self.volume = volume
        self._duck_level = duck_level
        self._music: AudioSource | None = None
        self._music_paused = False
        self._speech = bytearray()
        self._speech_lock = asyncio.Lock()
        # Текущий и целевой множитель громкости музыки (ducking).
        self._gain = 1.0
        self._ramp_step = (1.0 - duck_level) / max(1, _RAMP_MS // frame_ms)

    # ---------- речь ассистента ----------

    async def push_speech(self, pcm: bytes) -> None:
        """Добавляет кусок синтезированной речи (PCM s16le 48 кГц mono)."""
        async with self._speech_lock:
            self._speech.extend(pcm)

    async def drop_speech(self) -> None:
        """Обрывает текущую реплику — например, когда пользователь перебил."""
        async with self._speech_lock:
            self._speech.clear()

    @property
    def is_speaking(self) -> bool:
        return len(self._speech) > 0

    # ---------- музыка ----------

    async def set_music(self, source: AudioSource | None) -> None:
        old, self._music = self._music, source
        self._music_paused = False
        if old is not None:
            await old.close()

    @property
    def is_playing(self) -> bool:
        return self._music is not None and not self._music_paused

    def pause_music(self) -> None:
        self._music_paused = True

    def resume_music(self) -> None:
        self._music_paused = False

    # ---------- кадры ----------

    async def next_frame(self) -> bytes:
        """Один кадр 20 мс. Всегда возвращает данные — при тишине это нули."""
        speech = await self._take_speech()
        music = await self._take_music()

        # Цель ducking: пока звучит речь, музыка уходит на задний план.
        target = self._duck_level if speech is not None else 1.0
        self._gain = _approach(self._gain, target, self._ramp_step)

        mixed = np.zeros(self.frame_samples, dtype=np.float32)
        if music is not None:
            mixed += music.astype(np.float32) * self._gain
        if speech is not None:
            mixed += speech.astype(np.float32)

        mixed *= self.volume
        np.clip(mixed, -32768, 32767, out=mixed)
        return mixed.astype(np.int16).tobytes()

    async def _take_speech(self) -> np.ndarray | None:
        need = self.frame_samples * 2
        async with self._speech_lock:
            if not self._speech:
                return None
            # Хвост короче кадра дополняем тишиной, иначе разъедутся границы.
            chunk = bytes(self._speech[:need])
            del self._speech[:need]
        if len(chunk) < need:
            chunk = chunk + b"\x00" * (need - len(chunk))
        return np.frombuffer(chunk, dtype=np.int16)

    async def _take_music(self) -> np.ndarray | None:
        if self._music is None or self._music_paused:
            return None
        samples = await self._music.read(self.frame_samples)
        if samples is None:
            await self.set_music(None)
            return None
        return samples


def _approach(current: float, target: float, step: float) -> float:
    if current < target:
        return min(target, current + step)
    return max(target, current - step)
