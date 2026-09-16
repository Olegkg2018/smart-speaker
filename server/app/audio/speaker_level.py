"""Грубая оценка «тот же собеседник или нет» по громкости, не по голосу.

Не биометрия и не про тембр: только эвристика «настолько же громко или
тише — правдоподобно тот же человек; заметно тише — скорее всего другой
источник (другой человек дальше от микрофона, телевизор, фон)». Нужна
для окна продолжения разговора — там колонка слушает без нового
активационного слова, и что-то должно мешать случайному шуму включить
неверную реплику.

Ключевое: сравнивается уровень ДО автоусиления на плате (см.
firmware/main/audio_in.c, mic_level уходит из pcm[] до esp_agc_process).
Автоусиление специально выравнивает разницу в громкости источников —
то есть само по себе стирает как раз тот сигнал, который тут нужен.
"""

from __future__ import annotations


class SpeakerLevel:
    def __init__(self, min_ratio: float):
        self._min_ratio = min_ratio
        self._reference: float | None = None
        self._sum = 0.0
        self._count = 0

    def reset(self) -> None:
        """Новая реплика — копим заново; прошлый эталон трогаем отдельно."""
        self._sum = 0.0
        self._count = 0

    def observe(self, raw_level: float | None, is_speech: bool) -> None:
        """Учитывает уровень речевого кадра текущей реплики."""
        if raw_level is None or not is_speech:
            return
        self._sum += raw_level
        self._count += 1

    def capture_reference(self) -> None:
        """Реплика удачно закончилась — запомнить её уровень эталоном."""
        if self._count:
            self._reference = self._sum / self._count

    @property
    def has_reference(self) -> bool:
        return self._reference is not None

    def accepts(self, raw_level: float | None) -> bool:
        """Похоже ли на того же собеседника, что говорил перед ответом.

        Без эталона или без сырого уровня (устройство ещё не прислало ни
        одного mic_level) — не мешаем: фича должна быть строго аддитивной.
        """
        if raw_level is None or self._reference is None:
            return True
        return raw_level >= self._reference * self._min_ratio
