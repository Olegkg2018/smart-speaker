from pathlib import Path

from app.audio.vad import SilenceDetector
from app.wakeword import WakeWordDetector, WakeWordModel

_RATE = 16_000


async def test_missing_model_disables_detector_without_crashing(tmp_path):
    model = WakeWordModel(tmp_path / "нет-модели", "компьютер", _RATE, [])
    model.load()
    assert model.available is False
    wake = model.for_session()
    assert wake.available is False
    # Колонка должна продолжать работать на кнопке, а не падать.
    assert await wake.feed(b"\x00" * 640) is False
    wake.reset()


async def test_unloaded_detector_never_fires():
    wake = WakeWordModel(Path("/nonexistent"), "компьютер", _RATE, []).for_session()
    assert await wake.feed(b"\x01\x02" * 320) is False


def test_neighbours_land_in_grammar():
    model = WakeWordModel(Path("/tmp"), "Алиса", _RATE, ["Лариса", "Мелисса"])
    assert "алиса" in model.grammar
    assert "лариса" in model.grammar and "мелисса" in model.grammar
    # [unk] обязателен: без него распознаватель тянет любую речь к словарю.
    assert "[unk]" in model.grammar


def test_wake_word_is_normalised():
    model = WakeWordModel(Path("/tmp"), "  КомпьЮтер  ", _RATE, [])
    assert model.wake_word == "компьютер"


def _chunk(level: int, ms: int) -> bytes:
    import numpy as np

    return np.full(_RATE * ms // 1000, level, dtype=np.int16).tobytes()


def test_heard_speech_distinguishes_silence_from_finished_speech():
    vad = SilenceDetector(sample_rate=_RATE, threshold=400, silence_ms=900, min_speech_ms=200)
    assert vad.heard_speech is False

    for _ in range(15):  # 300 мс речи
        vad.feed(_chunk(2000, 20))
    assert vad.heard_speech is True

    vad.reset()
    assert vad.heard_speech is False


async def test_idle_reset_keeps_detector_fresh(monkeypatch, tmp_path):
    """Распознаватель глохнет от накопленного состояния — освежаем по таймеру."""
    import app.wakeword as ww

    wake = ww.WakeWordModel(tmp_path, "компьютер", _RATE, []).for_session()

    resets = []

    class FakeRec:
        def Reset(self):
            resets.append(1)

        def AcceptWaveform(self, pcm):
            return False

        def PartialResult(self):
            return '{"partial": ""}'

    wake._rec = FakeRec()
    wake.available = True
    wake._last_reset = 0.0  # как будто не сбрасывали очень давно

    chunk = b"\x00\x00" * (_RATE * 200 // 1000)  # ровно одна пачка
    assert await wake.feed(chunk) is False
    assert resets, "распознаватель должен был освежиться после долгого молчания"


async def test_no_reset_while_recently_refreshed(tmp_path):
    import time

    import app.wakeword as ww

    wake = ww.WakeWordModel(tmp_path, "компьютер", _RATE, []).for_session()
    resets = []

    class FakeRec:
        def Reset(self):
            resets.append(1)

        def AcceptWaveform(self, pcm):
            return False

        def PartialResult(self):
            return '{"partial": ""}'

    wake._rec = FakeRec()
    wake.available = True
    wake._last_reset = time.monotonic()  # только что сбрасывали

    chunk = b"\x00\x00" * (_RATE * 200 // 1000)
    assert await wake.feed(chunk) is False
    assert not resets, "лишний сброс стирает половину произнесённого слова"
