"""Синтез речи локально, через Piper."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from app.audio.resample import resample_pcm16

log = logging.getLogger(__name__)

# Конец предложения: точка/вопрос/восклицание, за которыми пробел или конец строки.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# Отдавать в синтез огрызки короче этого невыгодно — растёт накладной расход.
_MIN_CHUNK_CHARS = 12


class TextToSpeech:
    def __init__(self, voice_name: str, models_dir: Path, out_sample_rate: int):
        self._voice_name = voice_name
        self._models_dir = models_dir
        self._out_sample_rate = out_sample_rate
        self._voice = None
        self._native_rate = out_sample_rate

    def load(self) -> None:
        from piper import PiperVoice

        model_path = self._models_dir / f"{self._voice_name}.onnx"
        if not model_path.exists():
            raise FileNotFoundError(
                f"голос Piper не найден: {model_path}\n"
                f"Скачайте его: python -m piper.download_voices {self._voice_name} "
                f"--data-dir {self._models_dir}"
            )
        self._voice = PiperVoice.load(str(model_path))
        self._native_rate = self._voice.config.sample_rate
        log.info("голос Piper «%s» загружен (%d Гц)", self._voice_name, self._native_rate)

    async def synthesize(self, text: str) -> bytes:
        """Текст → PCM s16le mono на частоте микшера."""
        text = text.strip()
        if not text:
            return b""
        if self._voice is None:
            raise RuntimeError("голос не загружен, вызовите load()")
        pcm = await asyncio.to_thread(self._synthesize_sync, text)
        if self._native_rate != self._out_sample_rate:
            pcm = resample_pcm16(pcm, self._native_rate, self._out_sample_rate)
        return pcm

    async def synthesize_stream(self, text: str, on_chunk) -> None:
        """Тот же интерфейс, что у облачного синтеза.

        Piper считает фразу за доли секунды прямо на плате, поэтому дробить
        её на куски незачем — отдаём одним. Метод существует ради того, чтобы
        вызывающий код не различал локальный и облачный синтез.
        """
        pcm = await self.synthesize(text)
        if pcm:
            await on_chunk(pcm)

    def _synthesize_sync(self, text: str) -> bytes:
        chunks: list[bytes] = []
        # API Piper менялся между версиями: в новых `synthesize` отдаёт объекты
        # AudioChunk, в старых был `synthesize_stream_raw` с сырыми байтами.
        if hasattr(self._voice, "synthesize"):
            for chunk in self._voice.synthesize(text):
                chunks.append(getattr(chunk, "audio_int16_bytes", chunk))
        else:
            chunks.extend(self._voice.synthesize_stream_raw(text))
        return b"".join(chunks)


class SentenceBuffer:
    """Нарезает поток токенов от LLM на предложения.

    Это главный рычаг ощущаемой скорости: первое предложение уходит в синтез,
    пока модель ещё дописывает остальное, и колонка начинает говорить через
    доли секунды вместо нескольких секунд ожидания полного ответа.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, token: str) -> list[str]:
        """Добавляет кусок текста, возвращает готовые к синтезу предложения."""
        self._buffer += token
        parts = _SENTENCE_END.split(self._buffer)
        if len(parts) == 1:
            return []
        # Последний кусок — незаконченный, остаётся в буфере.
        self._buffer = parts[-1]
        ready = [p.strip() for p in parts[:-1] if p.strip()]
        return _merge_short(ready)

    def flush(self) -> str:
        """Остаток после конца потока."""
        tail, self._buffer = self._buffer.strip(), ""
        return tail


def _merge_short(sentences: list[str]) -> list[str]:
    """Склеивает слишком короткие фразы («Да.», «Хорошо.») с соседями."""
    merged: list[str] = []
    for sentence in sentences:
        if merged and len(merged[-1]) < _MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    return merged
