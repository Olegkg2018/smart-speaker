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
    timers: dict[str, asyncio.Task] = field(default_factory=dict)

    async def cancel_timers(self) -> None:
        for task in self.timers.values():
            task.cancel()
        self.timers.clear()
