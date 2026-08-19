"""Распознавание речи локально, через faster-whisper."""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

log = logging.getLogger(__name__)

# Короче этого — почти наверняка случайный тык кнопки, а не фраза.
_MIN_SPEECH_MS = 300


class SpeechToText:
    def __init__(
        self,
        model_name: str,
        device: str,
        compute_type: str,
        language: str,
        sample_rate: int,
        cpu_threads: int = 4,
    ):
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._sample_rate = sample_rate
        self._cpu_threads = cpu_threads
        self._model = None

    def load(self) -> None:
        """Загружает модель. Первый вызов скачивает её (сотни мегабайт)."""
        from faster_whisper import WhisperModel

        started = time.monotonic()
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            # На одноплатнике каждое ядро на счету: по умолчанию ctranslate2
            # берёт половину, а нам нужны все четыре.
            cpu_threads=self._cpu_threads,
        )
        log.info(
            "whisper «%s» загружена за %.1f с", self._model_name, time.monotonic() - started
        )

    async def transcribe(self, pcm: bytes) -> str:
        """PCM s16le mono на входе, распознанный текст на выходе."""
        min_bytes = self._sample_rate * _MIN_SPEECH_MS // 1000 * 2
        if len(pcm) < min_bytes:
            return ""
        if self._model is None:
            raise RuntimeError("модель не загружена, вызовите load()")

        # int16 → float32 в диапазоне [-1, 1], как ожидает whisper.
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # Модель считает на CPU и блокирует поток — уводим в executor.
        return await asyncio.to_thread(self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        started = time.monotonic()
        segments, _info = self._model.transcribe(
            audio,
            language=self._language,
            # Отсекает паузы и шум до того, как они попадут в декодер:
            # и быстрее, и меньше выдуманных слов на тишине.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            beam_size=1,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        log.info(
            "распознано за %.2f с (%.1f с аудио): %s",
            time.monotonic() - started,
            len(audio) / self._sample_rate,
            text or "<тишина>",
        )
        return text
