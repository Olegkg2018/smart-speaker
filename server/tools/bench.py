"""Замер скорости распознавания и синтеза на целевом железе.

Отвечает на единственный вопрос, который нельзя решить на ноутбуке: успевает
ли S905X3 распознавать речь быстрее, чем она произносится. Если RTF заметно
больше единицы, после каждой фразы будет висеть пауза — тогда нужна модель
поменьше.

Запуск в контейнере:
    docker compose exec happy python tools/bench.py
"""

from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.stt import SpeechToText
from app.tts import TextToSpeech

# Бытовые реплики, а не скороговорки: колонку спрашивают примерно так.
PHRASES = [
    "Какая сегодня погода в Москве?",
    "Включи что-нибудь бодрое.",
    "Поставь таймер на пятнадцать минут и расскажи, что нового.",
]


async def main() -> None:
    print(f"модель распознавания: {settings.whisper_model} "
          f"({settings.whisper_compute_type}, {settings.whisper_cpu_threads} потока)")

    # Синтез на 16 кГц: ровно та частота, на которой колонка шлёт микрофон.
    tts = TextToSpeech(settings.piper_voice, settings.models_dir, settings.mic_sample_rate)
    stt = SpeechToText(
        model_name=settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        language=settings.whisper_language,
        sample_rate=settings.mic_sample_rate,
        cpu_threads=settings.whisper_cpu_threads,
    )

    start = time.monotonic()
    tts.load()
    stt.load()
    print(f"загрузка моделей: {time.monotonic() - start:.1f} с\n")

    total_audio = 0.0
    total_stt = 0.0

    for phrase in PHRASES:
        start = time.monotonic()
        pcm = await tts.synthesize(phrase)
        tts_time = time.monotonic() - start

        seconds = len(pcm) / 2 / settings.mic_sample_rate
        start = time.monotonic()
        text = await stt.transcribe(pcm)
        stt_time = time.monotonic() - start

        total_audio += seconds
        total_stt += stt_time

        print(f"«{phrase}»")
        print(f"  синтез:      {tts_time:5.2f} с на {seconds:4.1f} с речи "
              f"(x{seconds / tts_time:.1f} быстрее реального времени)")
        print(f"  распознано:  {stt_time:5.2f} с, RTF {stt_time / seconds:.2f}")
        print(f"  услышано:    «{text}»\n")

    print(f"итого: {total_stt:.1f} с распознавания на {total_audio:.1f} с речи, "
          f"RTF {total_stt / total_audio:.2f}")
    print("RTF < 1 — распознавание успевает за речью; > 1 — после фразы будет пауза.")


if __name__ == "__main__":
    asyncio.run(main())
