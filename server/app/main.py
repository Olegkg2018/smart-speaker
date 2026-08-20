"""Точка входа: FastAPI с одним WebSocket-эндпоинтом для колонок."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.cloud_speech import CloudSpeechToText, CloudTextToSpeech
from app.config import settings
from app.screen import ScreenRenderer
from app.session import Session
from app.stt import SpeechToText
from app.tts import TextToSpeech
from app.wakeword import WakeWordModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("happy")

# httpx печатает полный URL каждого запроса, а в адресе Telegram лежит токен
# бота — он оказывался в логах открытым текстом. Нам эти строки не нужны:
# свои ошибки мы логируем сами и без секретов.
logging.getLogger("httpx").setLevel(logging.WARNING)

if settings.stt_provider == "openai":
    stt = CloudSpeechToText(
        api_key=settings.openai_api_key,
        model=settings.openai_stt_model,
        language=settings.whisper_language,
        sample_rate=settings.mic_sample_rate,
    )
else:
    stt = SpeechToText(
        model_name=settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        language=settings.whisper_language,
        sample_rate=settings.mic_sample_rate,
        cpu_threads=settings.whisper_cpu_threads,
    )

if settings.tts_provider == "openai":
    tts = CloudTextToSpeech(
        api_key=settings.openai_api_key,
        model=settings.openai_tts_model,
        voice=settings.openai_tts_voice,
        out_sample_rate=settings.out_sample_rate,
    )
else:
    tts = TextToSpeech(
        voice_name=settings.piper_voice,
        models_dir=settings.models_dir,
        out_sample_rate=settings.out_sample_rate,
    )
wake_model = WakeWordModel(
    model_dir=settings.vosk_model_dir,
    wake_word=settings.wake_word,
    sample_rate=settings.mic_sample_rate,
    extra_words=settings.wake_word_neighbours,
)
screen = ScreenRenderer(
    width=settings.screen_width,
    height=settings.screen_height,
    font_path=settings.screen_font,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Диагностика рывков звука: кадр обязан уходить каждые 20 мс, и когда
    # он опаздывает, виноват тот, кто надолго занял цикл событий. Python
    # умеет назвать такую корутину сам — включается переменной окружения,
    # потому что в обычной работе это лишний шум в логе.
    if settings.debug_slow_callbacks:
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        loop.slow_callback_duration = settings.debug_slow_callback_s
        log.warning(
            "включён поиск медленных корутин: порог %.0f мс",
            settings.debug_slow_callback_s * 1000,
        )

    # Модели грузятся один раз на старте: делать это в первой сессии значит
    # подарить пользователю несколько секунд тишины на первый же вопрос.
    log.info("загружаю модели…")
    # Whisper нужен только пути Claude: Realtime распознаёт речь сам, и грузить
    # его тогда — тратить память и минуту старта впустую.
    if settings.voice_provider != "openai_realtime":
        stt.load()
    else:
        log.info("voice_provider=openai_realtime — Whisper не загружаю")
    tts.load()
    if settings.screen_enabled:
        screen.load()
    if settings.wake_word_enabled:
        wake_model.load()
    if settings.voice_provider == "openai_realtime":
        if not settings.openai_api_key:
            log.warning("OPENAI_API_KEY не задан — агент работать не будет")
    elif not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY не задан — агент работать не будет")
    if "openai" in (settings.stt_provider, settings.tts_provider) and not settings.openai_api_key:
        log.warning("OPENAI_API_KEY не задан — облачные распознавание и синтез не заработают")
    log.info("сервер готов, слушаю %s:%d", settings.host, settings.port)
    yield


app = FastAPI(title="Happy Speaker", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    model = settings.openai_realtime_model if settings.voice_provider == "openai_realtime" else settings.model
    return {"status": "ok", "model": model}


@app.get("/stats")
async def stats() -> dict[str, object]:
    """Хватает ли железа: нагрузка, память и задержка цикла событий.

    Средняя загрузка меряется на всю плату, а не на контейнер: колонке мешает
    любой сосед, а не только она сама. Задержка цикла событий важнее обеих
    цифр — именно она рвёт звук, и она может расти даже на свободном
    процессоре, если какая-то операция надолго занимает поток.
    """
    import os
    import time as _time

    load1, load5, load15 = os.getloadavg()
    cores = os.cpu_count() or 1

    # Сколько на самом деле длится сон в 20 мс — столько же ждут и кадры звука.
    delays = []
    for _ in range(10):
        started = _time.monotonic()
        await asyncio.sleep(0.020)
        delays.append((_time.monotonic() - started - 0.020) * 1000)

    return {
        "status": "ok",
        "model": model_name(),
        "cores": cores,
        "load": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
        "load_per_core": round(load1 / cores, 2),
        "event_loop_delay_ms": {
            "avg": round(sum(delays) / len(delays), 1),
            "max": round(max(delays), 1),
        },
        "wake_word": settings.wake_word if settings.wake_word_enabled else None,
    }


def model_name() -> str:
    if settings.voice_provider == "openai_realtime":
        return settings.openai_realtime_model
    return settings.model


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()
    session = Session(
        ws,
        settings,
        stt,
        tts,
        screen if settings.screen_enabled else None,
        wake_model,
    )
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("сессия завершилась с ошибкой")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
