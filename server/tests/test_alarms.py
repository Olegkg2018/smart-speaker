from datetime import datetime, timedelta

from app.tools import alarms


def _at(hour: int, minute: int = 0, day_offset: int = 0) -> str:
    when = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (when + timedelta(days=day_offset)).isoformat()


def test_bare_time_in_the_past_means_tomorrow():
    """Сказанное вечером «в шесть» — это завтрашнее утро, а не прошедшее."""
    now = datetime(2026, 8, 20, 22, 0)
    resolved = alarms.resolve_time("06:00", now=now)
    assert resolved == datetime(2026, 8, 21, 6, 0)


def test_bare_time_later_today_stays_today():
    now = datetime(2026, 8, 20, 5, 0)
    assert alarms.resolve_time("06:00", now=now) == datetime(2026, 8, 20, 6, 0)


def test_full_date_is_respected():
    now = datetime(2026, 8, 20, 12, 0)
    assert alarms.resolve_time("2026-08-25T07:30", now=now) == datetime(2026, 8, 25, 7, 30)


def test_past_date_is_rejected():
    now = datetime(2026, 8, 20, 12, 0)
    # Будить задним числом бессмысленно — лучше честно не поставить.
    assert alarms.resolve_time("2026-08-19T07:00", now=now) is None


def test_nonsense_time_is_rejected():
    assert alarms.resolve_time("когда-нибудь") is None


async def test_alarm_survives_restart(tmp_path):
    """Ради этого будильники и лежат на диске: связь рвётся, утро — нет."""
    await alarms.set_alarm(tmp_path, "07:00", label="на работу")
    # Читаем заново, как после перезапуска сервера.
    restored = alarms.load(tmp_path)
    assert len(restored) == 1
    assert restored[0].label == "на работу"


async def test_sound_is_remembered(tmp_path):
    result = await alarms.set_alarm(tmp_path, "06:00", sound="шум дождя")
    assert "шум дождя" in result
    assert alarms.load(tmp_path)[0].sound == "шум дождя"


async def test_alarm_without_sound_says_nothing_about_it(tmp_path):
    result = await alarms.set_alarm(tmp_path, "06:00")
    assert "разбужу под" not in result


async def test_several_alarms_coexist(tmp_path):
    await alarms.set_alarm(tmp_path, "06:00")
    await alarms.set_alarm(tmp_path, "07:00")
    assert len(alarms.load(tmp_path)) == 2


async def test_cancel_removes_all(tmp_path):
    await alarms.set_alarm(tmp_path, "06:00")
    await alarms.cancel_alarms(tmp_path)
    assert alarms.load(tmp_path) == []


async def test_list_when_empty(tmp_path):
    assert "нет" in (await alarms.list_alarms(tmp_path)).lower()


async def test_drop_removes_only_one(tmp_path):
    await alarms.set_alarm(tmp_path, "06:00", label="первый")
    await alarms.set_alarm(tmp_path, "07:00", label="второй")
    first = alarms.load(tmp_path)[0]
    alarms.drop(tmp_path, first.id)
    left = alarms.load(tmp_path)
    assert len(left) == 1 and left[0].label == "второй"


def test_overdue_and_upcoming_are_separated(tmp_path):
    alarms.save(
        tmp_path,
        [
            alarms.Alarm(id="past", at=_at(6, day_offset=-1)),
            alarms.Alarm(id="future", at=_at(6, day_offset=1)),
        ],
    )
    overdue, upcoming = alarms.due_and_upcoming(tmp_path)
    assert [a.id for a in overdue] == ["past"]
    assert [a.id for a in upcoming] == ["future"]


def test_barely_late_alarm_is_not_dropped_as_overdue(tmp_path):
    """Реконнект колонки ровно в момент срабатывания — обычное дело.

    Будильник, просроченный на секунду, обязан остаться в «upcoming»:
    wait_and_fire() сам умеет разбудить сразу, если опоздание в пределах
    допуска. Раньше due_and_upcoming() резал по голому "в прошлом" и такой
    будильник молча выбрасывался в _restore_alarms(), даже не долетев до
    этой проверки.
    """
    alarms.save(
        tmp_path,
        [alarms.Alarm(id="just-late", at=(datetime.now() - timedelta(seconds=1)).isoformat())],
    )
    overdue, upcoming = alarms.due_and_upcoming(tmp_path)
    assert [a.id for a in overdue] == []
    assert [a.id for a in upcoming] == ["just-late"]


def test_broken_file_does_not_crash(tmp_path):
    (tmp_path / "alarms.json").write_text("не json", encoding="utf-8")
    assert alarms.load(tmp_path) == []


async def test_late_alarm_does_not_fire(tmp_path):
    """Сервер лежал всю ночь — будить в полдень уже незачем."""
    fired = []

    async def fire(alarm):
        fired.append(alarm)

    stale = alarms.Alarm(id="stale", at=(datetime.now() - timedelta(hours=3)).isoformat())
    await alarms.wait_and_fire(stale, fire)
    assert fired == []


async def test_due_alarm_fires(tmp_path):
    fired = []

    async def fire(alarm):
        fired.append(alarm)

    just_now = alarms.Alarm(id="now", at=(datetime.now() - timedelta(seconds=1)).isoformat())
    await alarms.wait_and_fire(just_now, fire)
    assert len(fired) == 1
