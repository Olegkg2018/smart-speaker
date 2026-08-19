import numpy as np

from app.audio.resample import resample_pcm16
from app.tts import SentenceBuffer
from app.tools.timers import _human


def test_sentence_buffer_holds_incomplete_sentence():
    buffer = SentenceBuffer()
    assert buffer.feed("Сейчас в Москве") == []
    assert buffer.feed(" двенадцать градусов") == []


def test_sentence_buffer_emits_on_terminator():
    buffer = SentenceBuffer()
    buffer.feed("Сейчас двенадцать градусов, пасмурно.")
    # Предложение отдаётся только когда стало ясно, что оно закончилось.
    ready = buffer.feed(" Днём потеплеет")
    assert ready == ["Сейчас двенадцать градусов, пасмурно."]


def test_sentence_buffer_flush_returns_tail():
    buffer = SentenceBuffer()
    buffer.feed("Готово")
    assert buffer.flush() == "Готово"
    assert buffer.flush() == ""


def test_sentence_buffer_merges_short_fragments():
    buffer = SentenceBuffer()
    ready = buffer.feed("Да. Конечно. Уже включаю твою любимую пластинку. ещё")
    # «Да.» отдельным вызовом синтеза — это лишняя задержка и рваная речь.
    assert ready == ["Да. Конечно.", "Уже включаю твою любимую пластинку."]


def test_resample_changes_length_proportionally():
    samples = np.zeros(22_050, dtype=np.int16)
    out = resample_pcm16(samples.tobytes(), 22_050, 48_000)
    assert len(out) // 2 == 48_000


def test_resample_preserves_constant_signal():
    samples = np.full(1000, 5_000, dtype=np.int16)
    out = np.frombuffer(resample_pcm16(samples.tobytes(), 22_050, 48_000), dtype=np.int16)
    assert out.min() >= 4_990 and out.max() <= 5_010


def test_russian_plurals_for_spoken_time():
    assert _human(1) == "1 секунду"
    assert _human(3) == "3 секунды"
    assert _human(11) == "11 секунд"
    assert _human(60) == "1 минуту"
    assert _human(300) == "5 минут"
    assert _human(90) == "1 минуту 30 секунд"
    assert _human(7200) == "2 часа"
