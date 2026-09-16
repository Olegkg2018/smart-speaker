from app.audio.speaker_level import SpeakerLevel


def test_no_reference_accepts_everything():
    sl = SpeakerLevel(min_ratio=0.5)
    assert sl.has_reference is False
    assert sl.accepts(1.0) is True
    assert sl.accepts(1000.0) is True


def test_missing_raw_level_always_accepted():
    sl = SpeakerLevel(min_ratio=0.5)
    sl.observe(1000.0, is_speech=True)
    sl.capture_reference()
    assert sl.accepts(None) is True


def test_reference_captured_from_speech_frames_only():
    sl = SpeakerLevel(min_ratio=0.5)
    sl.observe(1000.0, is_speech=True)
    sl.observe(10.0, is_speech=False)  # тишина/фон — не должна размывать эталон
    sl.observe(1000.0, is_speech=True)
    sl.capture_reference()
    assert sl.accepts(1000.0) is True
    assert sl.accepts(400.0) is False  # заметно тише — не тот же человек


def test_quieter_within_ratio_is_accepted():
    sl = SpeakerLevel(min_ratio=0.5)
    sl.observe(1000.0, is_speech=True)
    sl.capture_reference()
    assert sl.accepts(500.0) is True  # ровно на границе
    assert sl.accepts(499.0) is False  # чуть тише границы


def test_reset_keeps_reference_but_clears_accumulator():
    sl = SpeakerLevel(min_ratio=0.5)
    sl.observe(1000.0, is_speech=True)
    sl.capture_reference()
    sl.reset()
    assert sl.has_reference is True
    assert sl.accepts(600.0) is True
    # capture_reference без новых observe() ничего не меняет — эталон тот же.
    sl.capture_reference()
    assert sl.accepts(600.0) is True


def test_capture_reference_without_observations_keeps_old_reference():
    sl = SpeakerLevel(min_ratio=0.5)
    sl.observe(1000.0, is_speech=True)
    sl.capture_reference()
    sl.reset()
    sl.capture_reference()  # count == 0 — не затирает эталон мусором
    assert sl.accepts(500.0) is True
    assert sl.accepts(499.0) is False
