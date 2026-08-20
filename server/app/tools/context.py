"""Общий контекст, который инструменты получают от сессии."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.audio.mixer import AudioMixer
from app.config import Settings


@dataclass
class ToolContext:
    settings: Settings
    mixer: AudioMixer
    # Позволяет инструменту заговорить самому — например, когда сработал таймер.
    speak: Callable[[str], Awaitable[None]]
    now_playing: str | None = None
    # Что играть после текущего трека: плейлист заводится один раз, а
    # дальше колонка должна продолжать сама, без новой команды.
    queue: list[str] = field(default_factory=list)
    queue_name: str | None = None
    timers: dict[str, asyncio.Task] = field(default_factory=dict)

    def next_in_queue(self) -> str | None:
        """Следующий трек плейлиста, если он есть."""
        return self.queue.pop(0) if self.queue else None

    async def cancel_timers(self) -> None:
        for task in self.timers.values():
            task.cancel()
        self.timers.clear()
