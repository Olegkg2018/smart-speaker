"""Воспроизведение музыки: ffmpeg декодирует что угодно в PCM для микшера."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class PrebufferedSource:
    """Оборачивает источник, у которого уже прочитан ровно один кадр.

    play_music проверяет источник, читая из него первый кадр до того, как
    отдать его микшеру — иначе колонка бодро отрапортует «Играет», пока
    источник за спиной уже упал. Этот кадр нельзя терять, поэтому он
    возвращается как есть на первый read(), а дальше делегируется источнику.
    """

    def __init__(self, source, first_chunk: np.ndarray):
        self._source = source
        self._first_chunk: np.ndarray | None = first_chunk

    async def read(self, n_samples: int) -> np.ndarray | None:
        if self._first_chunk is not None:
            chunk, self._first_chunk = self._first_chunk, None
            return chunk
        return await self._source.read(n_samples)

    async def close(self) -> None:
        await self._source.close()


class FfmpegSource:
    """Тянет аудио из файла или сетевого потока и отдаёт PCM 48 кГц mono.

    ffmpeg сам разбирается с форматом, частотой и числом каналов, поэтому
    один и тот же класс годится и для mp3 с диска, и для потока с YouTube.

    Для YouTube ссылку в ffmpeg напрямую не отдать: прямой URL на CDN
    (googlevideo.com) подписан под конкретный клиент, которым его запросил
    yt-dlp, и без совпадающих заголовков сервер отвечает 403. Поэтому вместо
    URL сюда передаётся исходная ссылка на видео, а сам yt-dlp качает байты
    в stdout — ffmpeg только декодирует то, что уже прошло его авторизацию.
    """

    def __init__(self, url: str, sample_rate: int, title: str = ""):
        self.url = url
        self.title = title
        self._sample_rate = sample_rate
        self.is_stream = url.startswith("http://") or url.startswith("https://")
        self._ytdlp_proc: asyncio.subprocess.Process | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._eof = False

    async def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg не найден в PATH — установите его")

        # Сеть читает yt-dlp, ffmpeg — только локальный файл или его stdin,
        # поэтому сетевые флаги переподключения ему без надобности.
        ffmpeg_input = "pipe:0" if self.is_stream else self.url
        ffmpeg_cmd = [
            "ffmpeg",
            "-nostdin",
            "-i", ffmpeg_input,
            "-vn",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", str(self._sample_rate),
            "-loglevel", "error",
            "pipe:1",
        ]

        if not self.is_stream:
            self._proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            return

        if shutil.which("yt-dlp") is None:
            raise RuntimeError("yt-dlp не найден в PATH — установите его")

        # Реальный OS-пайп: StreamReader из одного asyncio-процесса нельзя
        # напрямую передать как stdin другому, а через файловый дескриптор — можно.
        read_fd, write_fd = os.pipe()
        try:
            self._ytdlp_proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "--format", "bestaudio",
                "--output", "-",
                "--quiet",
                "--no-warnings",
                "--no-playlist",
                self.url,
                stdout=write_fd,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(write_fd)  # дочерний процесс унаследовал дескриптор — родителю он больше не нужен

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=read_fd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(read_fd)

    async def read(self, n_samples: int) -> np.ndarray | None:
        if self._proc is None or self._proc.stdout is None or self._eof:
            return None
        need = n_samples * 2
        try:
            chunk = await self._proc.stdout.readexactly(need)
        except asyncio.IncompleteReadError as exc:
            self._eof = True
            if not exc.partial:
                await self._log_failure_if_any()
                return None
            # Последний неполный кадр дополняем тишиной.
            chunk = exc.partial + b"\x00" * (need - len(exc.partial))
        return np.frombuffer(chunk, dtype=np.int16)

    async def _log_failure_if_any(self) -> None:
        """Если поток оборвался почти сразу — это скорее ошибка, чем конец файла."""
        for proc, name in ((self._proc, "ffmpeg"), (self._ytdlp_proc, "yt-dlp")):
            if proc is None or proc.returncode in (None, 0):
                continue
            stderr = b""
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    stderr = await asyncio.wait_for(proc.stderr.read(), timeout=1)
            log.warning(
                "%s завершился с кодом %d при воспроизведении «%s»: %s",
                name, proc.returncode, self.title, stderr.decode(errors="replace")[:300],
            )

    async def close(self) -> None:
        self._eof = True
        for proc in (self._proc, self._ytdlp_proc):
            if proc is None or proc.returncode is not None:
                continue
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        self._proc = None
        self._ytdlp_proc = None


async def resolve_track(query: str, music_dir: Path) -> tuple[str, str] | None:
    """Ищет трек: сначала домашняя библиотека, потом интернет.

    Возвращает (url_или_путь, человекочитаемое название) либо None.
    """
    local = _search_local(query, music_dir)
    if local is not None:
        return str(local), local.stem
    return await _search_online(query)


# Порядок важен. YouTube с датацентровых адресов почти всегда отвечает 403 на
# само скачивание (поиск при этом работает — оттого сбой и выглядит странно:
# трек «найден», но не играет), а веб-клиенты требуют PO-token. SoundCloud
# отдаёт аудио без всего этого, поэтому идёт первым. YouTube оставлен как
# запасной: он оживёт, если появятся cookies или поставщик PO-token.
_SEARCH_PREFIXES = (("scsearch1", "SoundCloud"), ("ytsearch1", "YouTube"))

# SoundCloud отвечает примерно за семь секунд — запас нужен, иначе редкий
# выброс уводит поиск на YouTube, который найдёт трек, но не отдаст звук.
_SEARCH_TIMEOUT_S = 20.0


async def _search_online(query: str) -> tuple[str, str] | None:
    if shutil.which("yt-dlp") is None:
        log.warning("yt-dlp не установлен — поиск в интернете недоступен")
        return None
    for prefix, name in _SEARCH_PREFIXES:
        found = await _search_with(f"{prefix}:{query}", name)
        if found is not None:
            return found
    log.warning("в интернете ничего не нашлось по запросу «%s»", query)
    return None


_AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}


def _search_local(query: str, music_dir: Path) -> Path | None:
    if not music_dir.is_dir():
        return None
    words = [w for w in query.lower().split() if len(w) > 2]
    if not words:
        return None

    # Одного совпавшего слова мало: по запросу «Пикник Иероглиф» в фонотеке
    # найдётся любая другая песня Пикника, и до интернета дело не дойдёт.
    # При этом требовать все слова тоже нельзя — «Кино Группа крови» лежит
    # файлом «Виктор Цой - Группа крови», где слова «кино» нет.
    need = 1 if len(words) == 1 else max(2, (len(words) + 1) // 2)

    best: tuple[int, Path] | None = None
    for path in music_dir.rglob("*"):
        if path.suffix.lower() not in _AUDIO_SUFFIXES:
            continue
        haystack = str(path.relative_to(music_dir)).lower()
        score = sum(1 for w in words if w in haystack)
        if score >= need and (best is None or score > best[0]):
            best = (score, path)
    return best[1] if best else None


async def _search_with(search_query: str, source_name: str) -> tuple[str, str] | None:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        # Ссылка на страницу трека, а не на CDN: прямой URL потока подписан
        # под клиента yt-dlp и без совпадающих заголовков получает 403 —
        # скачивать сам поток тоже будет yt-dlp, при реальном воспроизведении.
        "--print", "%(webpage_url)s\n%(title)s",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        search_query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SEARCH_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        log.warning("%s не ответил за %.0f с", source_name, _SEARCH_TIMEOUT_S)
        return None

    if proc.returncode != 0:
        log.info("%s: ничего не нашлось (%s)", source_name, stderr.decode()[:120].strip())
        return None
    lines = stdout.decode().strip().splitlines()
    if len(lines) < 2:
        return None
    log.info("%s: нашёл «%s»", source_name, lines[1])
    return lines[0], lines[1]
