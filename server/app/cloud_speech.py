"""Распознавание и синтез речи через облако OpenAI.

Замена локальным Whisper и Piper там, где важнее скорость и качество голоса,
чем автономность. Интерфейс совпадает с `app.stt.SpeechToText` и
`app.tts.TextToSpeech`, поэтому остальной код не отличает облако от локали.

Зачем: на S905X3 локальный Whisper всегда считает тридцатисекундное окно и
тратит около пяти секунд на любую фразу (`tools/bench.py`), а Piper на слух
звучит роботом. Отправить аудио по сети и получить ответ обычно быстрее,
чем посчитать его на четырёх Cortex-A55.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import wave

from openai import AsyncOpenAI

from app.audio.resample import resample_pcm16

log = logging.getLogger(__name__)

# Короче этого — почти наверняка случайный тык кнопки, а не фраза.
_MIN_SPEECH_MS = 300

# Формат, который OpenAI TTS отдаёт без контейнера: сырой PCM16 моно 24 кГц.
_TTS_RATE = 24_000


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Оборачивает сырой PCM в WAV: API принимает файл, а не голые сэмплы."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # s16le
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class CloudSpeechToText:
    """Распознавание через whisper-1. Интерфейс как у app.stt.SpeechToText."""

    def __init__(self, api_key: str, model: str, language: str, sample_rate: int):
        self._client = AsyncOpenAI(api_key=api_key or None)
        self._model = model
        self._language = language
        self._sample_rate = sample_rate

    def load(self) -> None:
        # Ничего не грузим: модель живёт в облаке. Метод есть ради общего
        # интерфейса со локальным SpeechToText — main.py зовёт его вслепую.
        log.info("распознавание речи: облако OpenAI, модель «%s»", self._model)

    async def transcribe(self, pcm: bytes) -> str:
        min_bytes = self._sample_rate * _MIN_SPEECH_MS // 1000 * 2
        if len(pcm) < min_bytes:
            return ""

        started = time.monotonic()
        wav = _pcm_to_wav(pcm, self._sample_rate)
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                # Имя файла обязательно: по расширению API понимает формат.
                file=("speech.wav", wav, "audio/wav"),
                language=self._language,
            )
        except Exception:
            log.exception("облачное распознавание не удалось")
            return ""

        text = (result.text or "").strip()
        log.info(
            "распознано за %.2f с (%.1f с аудио): %s",
            time.monotonic() - started,
            len(pcm) / 2 / self._sample_rate,
            text or "<тишина>",
        )
        return text


class CloudTextToSpeech:
    """Синтез через tts-1. Интерфейс как у app.tts.TextToSpeech."""

    def __init__(self, api_key: str, model: str, voice: str, out_sample_rate: int):
        self._client = AsyncOpenAI(api_key=api_key or None)
        self._model = model
        self._voice = voice
        self._out_sample_rate = out_sample_rate

    def load(self) -> None:
        log.info("синтез речи: облако OpenAI, модель «%s», голос «%s»", self._model, self._voice)

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                # pcm — сырой s16le 24 кГц: не нужно ни распаковывать
                # контейнер, ни звать ffmpeg ради одной фразы.
                response_format="pcm",
            )
            pcm = response.content
        except Exception:
            log.exception("облачный синтез не удался")
            return b""

        return await asyncio.to_thread(resample_pcm16, pcm, _TTS_RATE, self._out_sample_rate)

    async def synthesize_stream(self, text: str, on_chunk) -> None:
        """Отдаёт куски звука по мере генерации, не дожидаясь конца фразы.

        Синтез целой фразы стоит около двух секунд, и всё это время колонка
        молчит. Потоковый ответ позволяет заговорить примерно втрое раньше:
        первые сэмплы уходят в микшер, пока модель ещё досинтезирует хвост.
        """
        text = text.strip()
        if not text:
            return

        # Ресемплинг идёт покусочно, поэтому куски должны быть кратны сэмплу
        # (2 байта), иначе на стыках появится треск от съехавших границ.
        tail = b""
        try:
            async with self._client.audio.speech.with_streaming_response.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="pcm",
            ) as response:
                async for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    data = tail + chunk
                    if len(data) % 2:
                        data, tail = data[:-1], data[-1:]
                    else:
                        tail = b""
                    if data:
                        await on_chunk(
                            resample_pcm16(data, _TTS_RATE, self._out_sample_rate)
                        )
        except Exception:
            log.exception("облачный потоковый синтез не удался")
