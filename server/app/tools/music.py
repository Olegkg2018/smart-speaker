"""Инструменты управления музыкой."""

from __future__ import annotations

import asyncio
import logging

from app.audio.player import FfmpegSource, PrebufferedSource, resolve_track
from app.tools.context import ToolContext

log = logging.getLogger(__name__)

# Столько ждём первых байтов, прежде чем поверить, что источник реально
# играет: YouTube умеет отдать ссылку и не отдать звук (403 от анти-бот
# защиты), и без проверки колонка соврёт «Играет».
# Файл с диска открывается мгновенно, а сетевому потоку нужно поднять
# yt-dlp, дождаться первых килобайт и раскрутить ffmpeg — на это уходят
# секунды, и слишком строгий срок рубит вполне живые треки.
_FIRST_CHUNK_TIMEOUT_LOCAL_S = 4.0
_FIRST_CHUNK_TIMEOUT_STREAM_S = 25.0


async def play_music(ctx: ToolContext, query: str) -> str:
    found = await resolve_track(query, ctx.settings.music_dir)
    if found is None:
        return f"Не нашёл ничего по запросу «{query}»."

    url, title = found
    source = FfmpegSource(url, ctx.settings.out_sample_rate, title)
    try:
        await source.start()
    except RuntimeError as exc:
        log.error("не удалось запустить воспроизведение: %s", exc)
        return "Не могу включить музыку: не настроен ffmpeg."

    # Ровно один кадр микшера: read() обязан отдавать n_samples сэмплов
    # за вызов, и это тот самый размер, который потом запросит микшер —
    # значит проверочный кусок можно целиком передать ему как есть.
    frame = ctx.settings.frame_samples_out
    timeout = (
        _FIRST_CHUNK_TIMEOUT_STREAM_S if source.is_stream else _FIRST_CHUNK_TIMEOUT_LOCAL_S
    )
    try:
        first_chunk = await asyncio.wait_for(source.read(frame), timeout=timeout)
    except TimeoutError:
        first_chunk = None
        log.warning("источник «%s» не отдал звук за %.0f с", title, timeout)

    if first_chunk is None:
        await source.close()
        return f"Не удалось включить «{title}» — источник не отвечает."

    await ctx.mixer.set_music(PrebufferedSource(source, first_chunk))
    ctx.now_playing = title
    return f"Играет: {title}"


async def control_playback(ctx: ToolContext, action: str) -> str:
    match action:
        case "pause":
            if not ctx.mixer.is_playing:
                return "Сейчас ничего не играет."
            ctx.mixer.pause_music()
            return "Пауза."
        case "resume":
            ctx.mixer.resume_music()
            return "Продолжаю."
        case "stop":
            await ctx.mixer.set_music(None)
            ctx.now_playing = None
            return "Выключил."
        case _:
            return f"Неизвестное действие: {action}"


async def set_volume(ctx: ToolContext, level: float) -> str:
    ctx.mixer.volume = max(0.0, min(1.0, level))
    return f"Громкость {round(ctx.mixer.volume * 100)} процентов."
