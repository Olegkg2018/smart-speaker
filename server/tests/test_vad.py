import numpy as np

from app.audio.vad import SilenceDetector

_RATE = 16_000


def _chunk(level: int, ms: int) -> bytes:
    n = _RATE * ms // 1000
    samples = np.full(n, level, dtype=np.int16)
    return samples.tobytes()


def _detector(**kw) -> SilenceDetector:
    defaults = dict(sample_rate=_RATE, threshold=400, silence_ms=900, min_speech_ms=200)
    defaults.update(kw)
    return SilenceDetector(**defaults)


def test_silence_before_any_speech_does_not_end_utterance():
    vad = _detector()
    for _ in range(50):  # 1 секунда тишины
        assert vad.feed(_chunk(0, 20)) is False


def test_speech_then_short_pause_does_not_end_utterance():
    vad = _detector(silence_ms=900, min_speech_ms=200)
    for _ in range(15):  # 300 мс речи — хватает на min_speech_ms
        assert vad.feed(_chunk(2000, 20)) is False
    for _ in range(10):  # 200 мс тишины — короче silence_ms
        assert vad.feed(_chunk(0, 20)) is False


def test_speech_then_sustained_silence_ends_utterance():
    vad = _detector(silence_ms=900, min_speech_ms=200)
    for _ in range(15):  # речь
        assert vad.feed(_chunk(2000, 20)) is False
    ended = False
    for _ in range(60):  # с запасом больше 900 мс
        if vad.feed(_chunk(0, 20)):
            ended = True
            break
    assert ended


def test_new_speech_resets_silence_counter():
    vad = _detector(silence_ms=900, min_speech_ms=200)
    for _ in range(15):
        vad.feed(_chunk(2000, 20))
    for _ in range(30):  # 600 мс тишины — почти дотянули до порога
        assert vad.feed(_chunk(0, 20)) is False
    vad.feed(_chunk(2000, 20))  # снова заговорили — счётчик тишины сбрасывается
    for _ in range(30):  # ещё 600 мс тишины — суммарно было бы больше 900, но не подряд
        assert vad.feed(_chunk(0, 20)) is False


def test_empty_chunk_is_ignored():
    vad = _detector()
    assert vad.feed(b"") is False


def test_reset_clears_state():
    vad = _detector(silence_ms=900, min_speech_ms=200)
    for _ in range(15):
        vad.feed(_chunk(2000, 20))
    vad.reset()
    for _ in range(50):  # без reset это давно закончило бы реплику
        assert vad.feed(_chunk(0, 20)) is False


def test_adaptive_threshold_follows_quiet_room():
    """В тихой комнате порог опускается — иначе не слышно речь издалека."""
    vad = _detector(threshold=150, adaptive=True, speech_factor=3.0)
    for _ in range(100):  # 2 секунды почти полной тишины
        vad.observe_noise(_chunk(10, 20))
    # Пол (150) не даёт порогу упасть в ноль, но и не задирает его.
    assert vad.threshold == 150


def test_adaptive_threshold_rises_in_noisy_room():
    vad = _detector(threshold=150, adaptive=True, speech_factor=3.0)
    for _ in range(200):  # шумный фон
        vad.observe_noise(_chunk(200, 20))
    # Порог поднялся выше пола: тихий шум больше не сойдёт за речь.
    assert vad.threshold > 150


def test_quiet_distant_speech_is_heard_when_room_is_silent():
    """Голос с двух метров тише — с фиксированным порогом он терялся."""
    vad = _detector(threshold=150, adaptive=True, speech_factor=3.0)
    for _ in range(50):
        vad.observe_noise(_chunk(5, 20))
    for _ in range(15):  # тихая, но различимая речь
        vad.feed(_chunk(300, 20))
    assert vad.heard_speech is True


def test_noise_estimate_survives_reset():
    vad = _detector(threshold=150, adaptive=True)
    for _ in range(200):
        vad.observe_noise(_chunk(200, 20))
    high = vad.threshold
    vad.reset()  # новая реплика — но комната та же
    assert vad.threshold == high


def test_fixed_threshold_mode_ignores_noise():
    vad = _detector(threshold=400, adaptive=False)
    for _ in range(200):
        vad.observe_noise(_chunk(300, 20))
    assert vad.threshold == 400
