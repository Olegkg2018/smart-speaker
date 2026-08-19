"""Таймеры. По срабатыванию колонка сама произносит напоминание."""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.tools.context import ToolContext

log = logging.getLogger(__name__)

_MAX_SECONDS = 24 * 3600


async def set_timer(ctx: ToolContext, seconds: int, label: str | None = None) -> str:
    if seconds <= 0:
        return "Нужно положительное время."
    if seconds > _MAX_SECONDS:
        return "Слишком долго — максимум сутки."

    timer_id = uuid.uuid4().hex[:8]
    ctx.timers[timer_id] = asyncio.create_task(_run(ctx, timer_id, seconds, label))
    return f"Таймер на {_human(seconds)} поставлен."


async def cancel_timers(ctx: ToolContext) -> str:
    count = len(ctx.timers)
    if not count:
        return "Активных таймеров нет."
    await ctx.cancel_timers()
    return "Отменил." if count == 1 else f"Отменил все {count}."


async def _run(ctx: ToolContext, timer_id: str, seconds: int, label: str | None) -> None:
    try:
        await asyncio.sleep(seconds)
        announcement = f"Таймер: {label}." if label else "Таймер сработал."
        await ctx.speak(announcement)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("таймер %s упал", timer_id)
    finally:
        ctx.timers.pop(timer_id, None)


def _human(seconds: int) -> str:
    """Склоняет время по-русски: колонка это произносит."""
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"
    if seconds >= 60:
        minutes = seconds // 60
        rest = seconds % 60
        text = f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}"
        if rest:
            text += f" {rest} {_plural(rest, 'секунду', 'секунды', 'секунд')}"
        return text
    return f"{seconds} {_plural(seconds, 'секунду', 'секунды', 'секунд')}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 100 in range(11, 15):
        return many
    match n % 10:
        case 1:
            return one
        case 2 | 3 | 4:
            return few
        case _:
            return many
