import numpy as np
import pytest

from app.audio.mixer import AudioMixer

FRAME = 960  # 20 мс на 48 кГц


class ToneSource:
    """Постоянный уровень — удобно измерять, что с ним сделал микшер."""

    def __init__(self, level: int = 10_000, frames: int | None = None):
        self.level = level
        self.left = frames

    async def read(self, n_samples: int):
        if self.left is not None:
            if self.left <= 0:
                return None
            self.left -= 1
        return np.full(n_samples, self.level, dtype=np.int16)

    async def close(self) -> None:
        pass


def make_mixer(**kwargs) -> AudioMixer:
    params = {
        "frame_samples": FRAME,
        "frame_ms": 20,
        "duck_level": 0.2,
        "listen_duck_level": 0.05,
        "volume": 1.0,
    }
    return AudioMixer(**(params | kwargs))


async def test_silence_when_idle():
    mixer = make_mixer()
    frame = await mixer.next_frame()
    assert len(frame) == FRAME * 2
    assert not np.any(np.frombuffer(frame, dtype=np.int16))


async def test_music_passes_through_at_full_volume():
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=10_000))
    samples = np.frombuffer(await mixer.next_frame(), dtype=np.int16)
    assert samples[0] == pytest.approx(10_000, abs=2)


async def test_volume_scales_output():
    mixer = make_mixer(volume=0.5)
    await mixer.set_music(ToneSource(level=10_000))
    samples = np.frombuffer(await mixer.next_frame(), dtype=np.int16)
    assert samples[0] == pytest.approx(5_000, abs=2)


async def test_speech_ducks_music_smoothly():
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=10_000))
    # Речь тише музыки, чтобы её вклад было легко отделить.
    await mixer.push_speech(np.full(FRAME * 20, 1_000, dtype=np.int16).tobytes())

    first = np.frombuffer(await mixer.next_frame(), dtype=np.int16)[0]
    # Первый кадр — только начало спада, а не мгновенный прыжок: иначе щелчок.
    assert first > 10_000 * 0.8

    for _ in range(10):
        await mixer.next_frame()
    settled = np.frombuffer(await mixer.next_frame(), dtype=np.int16)[0]
    # Музыка ушла на 20%, речь добавила свою тысячу.
    assert settled == pytest.approx(10_000 * 0.2 + 1_000, abs=50)


async def test_music_restores_after_speech_ends():
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=10_000))
    await mixer.push_speech(np.zeros(FRAME, dtype=np.int16).tobytes())

    await mixer.next_frame()  # речь кончилась в этом же кадре
    assert not mixer.is_speaking
    for _ in range(20):
        await mixer.next_frame()
    restored = np.frombuffer(await mixer.next_frame(), dtype=np.int16)[0]
    assert restored == pytest.approx(10_000, abs=50)


async def test_short_speech_tail_is_padded():
    mixer = make_mixer()
    # Половина кадра: микшер обязан дополнить тишиной, а не сдвинуть границы.
    await mixer.push_speech(np.full(FRAME // 2, 5_000, dtype=np.int16).tobytes())
    frame = await mixer.next_frame()
    assert len(frame) == FRAME * 2
    samples = np.frombuffer(frame, dtype=np.int16)
    assert samples[0] == pytest.approx(5_000, abs=2)
    assert samples[-1] == 0


async def test_finished_source_is_dropped():
    mixer = make_mixer()
    await mixer.set_music(ToneSource(frames=1))
    await mixer.next_frame()
    await mixer.next_frame()
    assert not mixer.is_playing


async def test_drop_speech_interrupts_reply():
    mixer = make_mixer()
    await mixer.push_speech(np.full(FRAME * 50, 5_000, dtype=np.int16).tobytes())
    await mixer.drop_speech()
    assert not mixer.is_speaking


async def test_listening_ducks_music_harder_than_speech():
    """Живой случай: эхоподавление на плате не вычитает громкую музыку
    полностью, и колонка слышала обрывки играющей песни как реплику
    пользователя — из-за этого невпопад крутила громкость. Пока идёт запись,
    музыка должна уходить тише, чем во время собственной речи ассистента."""
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=10_000))
    mixer.set_listening(True)

    for _ in range(30):
        await mixer.next_frame()
    settled = np.frombuffer(await mixer.next_frame(), dtype=np.int16)[0]
    assert settled == pytest.approx(10_000 * 0.05, abs=50)


async def test_listening_stops_ducking_once_cleared():
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=10_000))
    mixer.set_listening(True)
    for _ in range(30):
        await mixer.next_frame()
    mixer.set_listening(False)

    for _ in range(30):
        await mixer.next_frame()
    restored = np.frombuffer(await mixer.next_frame(), dtype=np.int16)[0]
    assert restored == pytest.approx(10_000, abs=50)


async def test_assistant_speech_ducks_more_than_listening():
    """Пока ассистент говорит сам, приоритет — его duck_level, даже если
    флаг прослушивания почему-то остался включён."""
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=10_000))
    mixer.set_listening(True)
    await mixer.push_speech(np.full(FRAME * 30, 1_000, dtype=np.int16).tobytes())

    for _ in range(20):
        await mixer.next_frame()
    settled = np.frombuffer(await mixer.next_frame(), dtype=np.int16)[0]
    assert settled == pytest.approx(10_000 * 0.2 + 1_000, abs=50)


async def test_clipping_is_bounded():
    mixer = make_mixer()
    await mixer.set_music(ToneSource(level=30_000))
    await mixer.push_speech(np.full(FRAME, 30_000, dtype=np.int16).tobytes())
    samples = np.frombuffer(await mixer.next_frame(), dtype=np.int16)
    # Сумма превышает диапазон int16 — важно, что она обрезана, а не перевернулась.
    assert samples.max() > 0
    assert samples.max() <= 32767
