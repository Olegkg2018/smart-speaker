"""Детектор конца реплики по тишине.

Кнопка и активационное слово только начинают запись; момент, когда человек
замолчал, определяет этот детектор.

Порог адаптивный, и это не украшение. С фиксированным порогом колонка
слышит только того, кто стоит вплотную к микрофону: голос с двух метров
приходит в разы тише, детектор считает его тишиной и обрывает реплику,
не дождавшись ни слова. Поэтому уровень фона измеряется на ходу, а речью
считается всё, что заметно громче него.
"""

from __future__ import annotations

import numpy as np


class SilenceDetector:
    def __init__(
        self,
        sample_rate: int,
        threshold: int,
        silence_ms: int,
        min_speech_ms: int,
        adaptive: bool = True,
        speech_factor: float = 3.0,
    ):
        # threshold в адаптивном режиме работает нижней границей: тише этого
        # речью не считаем никогда, иначе в полной тишине сойдёт любой шорох.
        self._floor = threshold
        self._adaptive = adaptive
        self._speech_factor = speech_factor
        self._silence_samples_needed = sample_rate * silence_ms // 1000
        self._min_speech_samples = sample_rate * min_speech_ms // 1000
        self._silence_samples = 0
        self._speech_samples = 0
        self._noise = float(threshold)

    def reset(self) -> None:
        self._silence_samples = 0
        self._speech_samples = 0
        # Оценку фона не сбрасываем: она про комнату, а не про реплику.

    @property
    def heard_speech(self) -> bool:
        """Была ли вообще речь. Отличает «человек молчит» от «человек договорил»."""
        return self._speech_samples >= self._min_speech_samples

    @property
    def threshold(self) -> float:
        if not self._adaptive:
            return float(self._floor)
        return max(self._floor, self._noise * self._speech_factor)

    def observe_noise(self, pcm: bytes) -> None:
        """Слушать фон, пока запись не идёт.

        К началу реплики детектор уже знает, насколько тихо в комнате, и не
        тратит первые секунды на калибровку вслепую.
        """
        if not self._adaptive:
            return
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return
        level = float(np.abs(samples.astype(np.int32)).mean())
        # Речь мимо колонки не должна задирать оценку фона.
        if level < self.threshold:
            self._noise += (level - self._noise) * 0.05

    def feed(self, pcm: bytes) -> bool:
        """Кормит очередной кусок PCM s16le mono. True — реплика закончена."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return False

        level = float(np.abs(samples.astype(np.int32)).mean())
        is_speech = level >= self.threshold

        if not is_speech:
            # Фон подтягиваем только по тихим кускам и медленно: иначе
            # собственная речь поднимет порог и оглушит детектор.
            self._noise += (level - self._noise) * 0.05

        if is_speech:
            self._speech_samples += samples.size
            self._silence_samples = 0
            return False

        # Тишину в начале (до того как вообще заговорили) концом не считаем —
        # иначе первая же пауза на вдох перед фразой обрежет запись.
        if self._speech_samples < self._min_speech_samples:
            return False

        self._silence_samples += samples.size
        return self._silence_samples >= self._silence_samples_needed
