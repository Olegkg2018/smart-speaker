"""Музыка не должна задерживать кадр в динамик.

Живой случай: при старте трека ffmpeg (а для сети ещё и yt-dlp) отдаёт
данные неровно, и раньше read() ждал их прямо внутри кадра микшера —
87 из 224 кадров опоздали, худшее опоздание 259 мс. Теперь чтение идёт
впрок отдельной задачей, а на просвет отдаётся тишина.
"""

import asyncio

import numpy as np

from app.audio.player import FfmpegSource

FRAME = 960  # 20 мс на 48 кГц


class _SlowStdout:
    """Поток, который «задумывается» перед первой выдачей, как ffmpeg на старте."""

    def __init__(self, delay: float, chunks: int):
        self._delay = delay
        self._left = chunks
        self.reads = 0

    async def readexactly(self, n: int) -> bytes:
        self.reads += 1
        if self._left <= 0:
            raise asyncio.IncompleteReadError(b"", n)
        self._left -= 1
        await asyncio.sleep(self._delay)
        return b"\x01\x00" * (n // 2)


def _source_with(stdout) -> FfmpegSource:
    src = FfmpegSource("/tmp/x.mp3", 48_000, title="тест")
    # Подменяем процесс: нам нужен только его stdout.
    src._proc = type("P", (), {"stdout": stdout, "returncode": 0, "stderr": None})()
    return src


async def test_slow_source_does_not_delay_frame():
    """Пока источник раскачивается, кадр возвращается сразу — тишиной."""
    src = _source_with(_SlowStdout(delay=0.2, chunks=5))

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    frame = await src.read(FRAME)
    elapsed = loop.time() - t0

    # Главное: не ждали 200 мс вместе с ffmpeg.
    assert elapsed < 0.05, f"кадр задержался на {elapsed*1000:.0f} мс"
    assert frame is not None and len(frame) == FRAME
    assert not frame.any(), "на просвете ожидалась тишина"

    await src.close()


async def test_buffered_audio_is_returned_after_it_arrives():
    """Как только данные накопились, отдаём именно их, а не тишину."""
    src = _source_with(_SlowStdout(delay=0.01, chunks=5))

    await src.read(FRAME)  # запускает фоновое чтение
    await asyncio.sleep(0.1)  # даём наполнить буфер

    frame = await src.read(FRAME)
    assert frame is not None and frame.any(), "ожидался реальный звук из буфера"

    await src.close()


async def test_end_of_stream_reports_none():
    """Когда источник кончился и буфер пуст — это конец трека, а не тишина."""
    src = _source_with(_SlowStdout(delay=0.0, chunks=2))

    await src.read(FRAME)
    await asyncio.sleep(0.05)  # фоновая задача успевает дойти до конца

    seen_none = False
    for _ in range(10):
        if await src.read(FRAME) is None:
            seen_none = True
            break
    assert seen_none, "конец потока не был замечен"

    await src.close()


async def test_close_stops_background_reader():
    src = _source_with(_SlowStdout(delay=0.01, chunks=1000))
    await src.read(FRAME)
    assert src._filler is not None

    await src.close()
    assert src._filler is None, "фоновое чтение пережило закрытие источника"
