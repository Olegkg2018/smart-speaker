"""Точка входа: FastAPI с одним WebSocket-эндпоинтом для колонок."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.audio import music_index
from app.cloud_speech import CloudSpeechToText, CloudTextToSpeech
from app.config import settings
from app import satellite_page, webui
from app.screen import ScreenRenderer
from app.session import Session
from app.stt import SpeechToText
from app.tts import TextToSpeech

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
screen = ScreenRenderer(
    width=settings.screen_width,
    height=settings.screen_height,
    font_path=settings.screen_font,
)

# Во сколько ночи (местное время контейнера — TZ выставлен в
# docker-compose.yml) пересчитывать теги фонотеки. Три часа не пересекается
# ни с разговором, ни с будильниками на утро.
_MUSIC_INDEX_HOUR = 3


async def _music_index_loop() -> None:
    """Раз в сутки размечает фонотеку по жанру/настроению/поводу.

    Без этого «поставь весёлую музыку» или «шум дождя» находит трек только
    если это слово случайно оказалось в имени файла — см. app.audio.music_index.
    """
    if not settings.openai_api_key:
        log.info("OPENAI_API_KEY не задан — разметка фонотеки по настроению отключена")
        return

    # Первый прогон — сразу, а не только следующей ночью: иначе на свежем
    # сервере индекс пустует до утра.
    if not music_index.index_file_path(settings.music_index_dir).exists():
        try:
            await music_index.refresh(
                settings.music_dir,
                settings.music_index_dir,
                settings.openai_api_key,
                settings.web_search_model,
            )
        except Exception:
            log.exception("не удалось построить индекс фонотеки")

    while True:
        now = datetime.now()
        next_run = now.replace(hour=_MUSIC_INDEX_HOUR, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await music_index.refresh(
                settings.music_dir,
                settings.music_index_dir,
                settings.openai_api_key,
                settings.web_search_model,
            )
        except Exception:
            log.exception("не удалось обновить индекс фонотеки")


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
    if settings.voice_provider == "openai_realtime":
        if not settings.openai_api_key:
            log.warning("OPENAI_API_KEY не задан — агент работать не будет")
    elif not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY не задан — агент работать не будет")
    if "openai" in (settings.stt_provider, settings.tts_provider) and not settings.openai_api_key:
        log.warning("OPENAI_API_KEY не задан — облачные распознавание и синтез не заработают")

    music_index_task = asyncio.create_task(_music_index_loop())
    log.info("сервер готов, слушаю %s:%d", settings.host, settings.port)
    yield
    music_index_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await music_index_task


app = FastAPI(title="Happy Speaker", lifespan=lifespan)

# Страница управления: посмотреть будильники, списки, заметки и
# память, убрать лишнее. Голосом всё это ставится, но не смотрится.
app.include_router(webui.router)
# Сателлит для телефона: страница, а не приложение — Android SDK ради
# проверки идеи не нужен, а браузер даёт аппаратное эхоподавление.
app.include_router(satellite_page.router)


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
    }


def model_name() -> str:
    if settings.voice_provider == "openai_realtime":
        return settings.openai_realtime_model
    return settings.model


# Одна сессия на комнату: колонка и телефон рядом — это два микрофона
# одного ассистента, а не два ассистента. Разговор, память и ответ общие,
# слушает тот, кто слышит громче.
_rooms: dict[str, Session] = {}
_rooms_lock = asyncio.Lock()


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()

    # Комнату и роль знаем только из hello, поэтому читаем его здесь, до
    # того как решать, к какой сессии присоединять.
    try:
        hello = json.loads(await ws.receive_text())
    except Exception:
        log.warning("устройство не представилось — закрываю")
        with contextlib.suppress(Exception):
            await ws.close()
        return

    room = str(hello.get("room", "home"))
    async with _rooms_lock:
        session = _rooms.get(room)
        if session is None:
            session = Session(settings, stt, tts, screen if settings.screen_enabled else None)
            _rooms[room] = session

    try:
        await session.serve(ws, hello)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("сессия завершилась с ошибкой")
    finally:
        async with _rooms_lock:
            # Комната живёт, пока в ней есть хоть одно устройство.
            if _rooms.get(room) is session and session.is_empty:
                del _rooms[room]


def _build_tls_app() -> FastAPI:
    """Тот же /satellite, /stream и /, но поверх HTTPS — для айфона.

    Отдельный FastAPI-объект без своего lifespan: модели (stt/tts/screen)
    грузит только основной `app`, и оба сервера в одном процессе делят те
    же объекты через модульные переменные (`stream` использует те же
    `_rooms`, `stt`, `tts`, что и обычный вход) — второй лишний прогон
    lifespan здесь только загрузил бы модели заново и завёл вторую задачу
    ночной переиндексации фонотеки.

    `webui.router` подключён по той же причине, что и `satellite_page`:
    у него нет своего состояния, только чтение/запись файлов из settings.
    Без него навигация "Управление" на странице сателлита при заходе по
    HTTPS упиралась в 404 — а стандартный ответ FastAPI на 404 приходит
    как JSON, и браузер вместо страницы предлагает скачать файл.
    """
    tls_app = FastAPI(title="Happy Speaker (TLS)")
    tls_app.include_router(webui.router)
    tls_app.include_router(satellite_page.router)
    tls_app.add_api_websocket_route("/stream", stream)
    return tls_app


def main() -> None:
    import uvicorn

    # Пинг WebSocket, иначе полуоткрытое соединение висит бесконечно долго.
    # Живой случай: колонка потеряла питание и переподключилась заново, но
    # старый сокет не получил ни FIN, ни RST (роутер/NAT промолчал) — сервер
    # без явной проверки живости держал бы его часами (стандартный TCP
    # keepalive в Linux — 2 часа), а новое подключение того же устройства
    # добавлялось вторым в ту же комнату. С пингом раз в 15 секунд мёртвый
    # сокет закрывается сервером сам за 30 секунд без ответа.
    ws_ping = {"ws_ping_interval": 15.0, "ws_ping_timeout": 15.0}

    servers = [uvicorn.Server(uvicorn.Config(
        app, host=settings.host, port=settings.port, log_level="warning", **ws_ping,
    ))]

    # Браузер открывает микрофон только на защищённой странице. На Android
    # это обходится флагом браузера, на iOS (WebKit) такого флага нет —
    # там работает только настоящий HTTPS. Поднимаем его вторым сервером,
    # рядом с обычным: колонка как ходила по ws:// на port, так и ходит.
    if settings.tls_cert and settings.tls_key:
        cert, key = Path(settings.tls_cert), Path(settings.tls_key)
        if cert.exists() and key.exists():
            servers.append(uvicorn.Server(uvicorn.Config(
                _build_tls_app(), host=settings.host, port=settings.tls_port,
                ssl_certfile=str(cert), ssl_keyfile=str(key),
                log_level="warning", **ws_ping,
            )))
            log.warning(
                "HTTPS для сателлита на https://<адрес>:%d/satellite "
                "(колонка по-прежнему на ws://<адрес>:%d)",
                settings.tls_port, settings.port,
            )
        else:
            log.warning("сертификат не найден (%s), поднимаюсь без HTTPS", cert)

    async def _serve_all() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(_serve_all())


if __name__ == "__main__":
    main()
