from app.audio.mixer import AudioMixer
from app.config import Settings
from app.tools.context import ToolContext


def _ctx():
    settings = Settings()
    mixer = AudioMixer(frame_samples=960, frame_ms=20)
    return ToolContext(settings=settings, mixer=mixer, speak=None)


def test_queue_starts_empty():
    assert _ctx().next_in_queue() is None


def test_queue_returns_tracks_in_order():
    ctx = _ctx()
    ctx.queue = ["первая", "вторая"]
    assert ctx.next_in_queue() == "первая"
    assert ctx.next_in_queue() == "вторая"
    # Плейлист кончился — колонка не должна пытаться играть дальше.
    assert ctx.next_in_queue() is None


def test_each_session_has_its_own_queue():
    a, b = _ctx(), _ctx()
    a.queue.append("трек")
    assert b.queue == [], "очередь протекла между сессиями"
