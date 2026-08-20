"""Будильники и напоминания на конкретное время.

Отличие от таймера принципиальное, и оно в надёжности. Таймер живёт внутри
сессии: оборвалась связь — он исчез вместе с ней. Для «десяти минут пока
варится» это терпимо, для будильника на утро — нет: колонка переподключается
регулярно, и обещанное пробуждение молча не случится.

Поэтому будильники лежат на диске рядом со списками и восстанавливаются при
каждом подключении. Сработавшие удаляются, пропущенные (сервер лежал в это
время) — тоже, но с записью в лог: разбудить задним числом уже нельзя.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Насколько поздно сработавший будильник ещё имеет смысл. Если сервер был
# выключен полчаса, будить уже поздно — человек проснулся сам.
_LATE_TOLERANCE = timedelta(minutes=5)


@dataclass
class Alarm:
    id: str
    at: str  # ISO-время, местное
    label: str | None = None
    sound: str | None = None  # чем будить: «шум дождя», иначе просто голосом

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)


def _path(alarms_dir: Path) -> Path:
    return alarms_dir / "alarms.json"


def load(alarms_dir: Path) -> list[Alarm]:
    path = _path(alarms_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Alarm(**item) for item in data]
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        log.warning("не удалось прочитать будильники: %s", exc)
        return []


def save(alarms_dir: Path, alarms: list[Alarm]) -> bool:
    try:
        alarms_dir.mkdir(parents=True, exist_ok=True)
        tmp = _path(alarms_dir).with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(a) for a in alarms], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(_path(alarms_dir))
        return True
    except OSError as exc:
        log.warning("не удалось сохранить будильники: %s", exc)
        return False


def resolve_time(when: str, now: datetime | None = None) -> datetime | None:
    """Превращает «06:00» или «2026-08-21T06:00» в конкретный момент.

    Голое время без даты — это ближайшее такое время в будущем: сказанное
    вечером «в шесть» означает завтрашнее утро, а не сегодняшнее прошедшее.
    """
    now = now or datetime.now()
    text = when.strip()

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            hh, mm = (int(part) for part in text.split(":")[:2])
        except (ValueError, TypeError):
            return None
        parsed = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if parsed <= now:
            parsed += timedelta(days=1)
        return parsed

    # Дата пришла целиком, но всё равно в прошлом — будить уже поздно.
    return parsed if parsed > now else None


async def set_alarm(
    alarms_dir: Path, when: str, label: str | None = None, sound: str | None = None
) -> str:
    at = resolve_time(when)
    if at is None:
        return "Не понял, на какое время ставить."

    alarm = Alarm(id=uuid.uuid4().hex[:8], at=at.isoformat(), label=label, sound=sound)
    alarms = load(alarms_dir)
    alarms.append(alarm)
    if not save(alarms_dir, alarms):
        return "Не смог сохранить будильник."

    when_text = at.strftime("%H:%M")
    day = "завтра" if at.date() > datetime.now().date() else "сегодня"
    how = f", разбужу под {sound}" if sound else ""
    return f"Будильник на {day} в {when_text} поставлен{how}."


async def list_alarms(alarms_dir: Path) -> str:
    alarms = sorted(load(alarms_dir), key=lambda a: a.at)
    if not alarms:
        return "Будильников нет."
    parts = []
    for a in alarms:
        day = "завтра" if a.when.date() > datetime.now().date() else "сегодня"
        parts.append(f"{day} в {a.when.strftime('%H:%M')}" + (f" — {a.label}" if a.label else ""))
    return "Будильники: " + "; ".join(parts) + "."


async def cancel_alarms(alarms_dir: Path) -> str:
    alarms = load(alarms_dir)
    if not alarms:
        return "Будильников и так нет."
    if not save(alarms_dir, []):
        return "Не смог отменить."
    return "Отменил." if len(alarms) == 1 else f"Отменил все {len(alarms)}."


def drop(alarms_dir: Path, alarm_id: str) -> None:
    save(alarms_dir, [a for a in load(alarms_dir) if a.id != alarm_id])


def due_and_upcoming(alarms_dir: Path) -> tuple[list[Alarm], list[Alarm]]:
    """Делит будильники на просроченные и те, что ещё впереди."""
    now = datetime.now()
    overdue, upcoming = [], []
    for alarm in load(alarms_dir):
        (overdue if alarm.when <= now else upcoming).append(alarm)
    return overdue, upcoming


async def wait_and_fire(alarm: Alarm, fire) -> None:
    """Ждёт своего времени и будит.

    Ждём по календарю, а не отсчётом секунд: за ночь плата может уснуть или
    подтянуть время по сети, и накопленный отсчёт разъедется с реальностью.
    """
    while True:
        left = (alarm.when - datetime.now()).total_seconds()
        if left <= 0:
            break
        # Просыпаемся хотя бы раз в минуту и сверяемся с часами заново.
        await asyncio.sleep(min(left, 60))

    if datetime.now() - alarm.when > _LATE_TOLERANCE:
        log.warning("будильник %s просрочен, будить поздно", alarm.id)
        return
    await fire(alarm)
