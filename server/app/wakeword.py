"""Локальный поиск активационного слова через Vosk.

Смысл в деньгах: до активации звук не покидает дом. Колонка непрерывно шлёт
микрофон на сервер по локальной сети, здесь он бесплатно проверяется на одно
слово, и только после этого начинается платный разговор с облаком.

Распознавание идёт по грамматике из одного слова, а не полное: так на порядок
быстрее и точнее. Слово стоит выбирать подлиннее — короткие притягивают
созвучия («Алиса» срабатывает на «Ларису»), а «Компьютер» на замерах не дал
ни одного ложного срабатывания.

Модель одна на весь сервер (она весит под сотню мегабайт), а распознаватель —
свой у каждой сессии: KaldiRecognizer хранит состояние и не рассчитан на
два одновременных потока. Общий на всех он ломается ровно в момент
переподключения колонки, когда старая сессия ещё жива, а новая уже кормит
его своим аудио.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Кадр с микрофона — 20 мс, а Vosk на этой плате тратит на него около семи
# миллисекунд процессора. Гонять его на каждый кадр значит держать event loop
# занятым почти половину времени: WebSocket перестаёт успевать с пингами и
# соединение рвётся. Поэтому копим пачку и считаем её разом, в отдельном потоке.
_BATCH_MS = 200

# Распознаватель копит внутреннее состояние и за часы непрерывной тишины
# перестаёт слышать слово вовсе. Сам он сбрасывается только при удачном
# срабатывании, поэтому в фоновом режиме его нужно освежать принудительно.
_IDLE_RESET_S = 60.0

# Грамматика узкая, и распознаватель обязан отнести услышанное к чему-то из
# словаря: без балласта обычная речь и слова из песен притягиваются прямо к
# активационной фразе. На замерах эти слова убрали последние ложные
# срабатывания — «Слушай, я потом перезвоню» перестало будить колонку.
_FILLER_WORDS = [
    "потом", "перезвоню", "музыку", "слушает", "хорошо", "ладно", "давай",
    "сейчас", "который", "нужно", "нужен", "новый", "стоит", "столе",
    "целыми", "днями", "очень", "вкусный", "компот", "вишни", "минуту",
    "спасибо", "пожалуйста", "конечно", "наверное", "просто", "может",
    "человек", "работает", "думаю", "знаешь", "смотри", "погоди",
]


class WakeWordModel:
    """Модель Vosk, общая на весь сервер. Раздаёт сессиям распознаватели."""

    def __init__(self, model_dir: Path, wake_word: str, sample_rate: int, extra_words: list[str]):
        self._model_dir = model_dir
        self.wake_word = wake_word.lower().strip()
        self.sample_rate = sample_rate
        # Фраза из двух слов («слухай компьютер») ловится куда надёжнее
        # одиночного слова: случайное созвучие в речи или в песне совпадёт
        # с одним словом легко, а с двумя подряд — почти никогда.
        self.wake_parts = self.wake_word.split()
        # Созвучия в словаре дают распознавателю куда деть похожие слова,
        # иначе он тянет их к единственному known-варианту — к нашему.
        # В грамматику слова фразы идут по отдельности: Vosk работает
        # словарём, а не строками.
        vocabulary = [
            *self.wake_parts,
            *(w.lower() for w in extra_words),
            *_FILLER_WORDS,
            "[unk]",
        ]
        self.grammar = json.dumps(sorted(set(vocabulary)), ensure_ascii=False)
        self._model = None
        self.available = False

    def load(self) -> None:
        if not self._model_dir.is_dir():
            log.warning(
                "модель Vosk не найдена в %s — активация голосом отключена, останется кнопка",
                self._model_dir,
            )
            return
        try:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)  # иначе Kaldi засыпает лог отладкой на каждый кадр
            started = time.monotonic()
            self._model = Model(str(self._model_dir))
            self.available = True
            log.info(
                "активационное слово «%s» готово (загрузка %.1f с)",
                self.wake_word,
                time.monotonic() - started,
            )
        except Exception:
            log.exception("не удалось поднять поиск активационного слова")

    def new_recognizer(self):
        from vosk import KaldiRecognizer

        return KaldiRecognizer(self._model, self.sample_rate, self.grammar)

    def for_session(self) -> WakeWordDetector:
        return WakeWordDetector(self)


class WakeWordDetector:
    """Распознаватель одной сессии. Живёт и умирает вместе с ней."""

    def __init__(self, model: WakeWordModel):
        self._model = model
        self._wake_parts = model.wake_parts
        self._rec = model.new_recognizer() if model.available else None
        self.available = model.available
        self._batch = bytearray()
        self._batch_bytes = model.sample_rate * _BATCH_MS // 1000 * 2
        self._busy = False
        self._last_reset = time.monotonic()

    def reset(self) -> None:
        """Забыть накопленное — после активации и после каждой реплики."""
        self._batch.clear()
        self._last_reset = time.monotonic()
        if self._rec is not None:
            self._rec.Reset()

    async def feed(self, pcm: bytes) -> bool:
        """Кормит кусок PCM s16le. True — слово прозвучало."""
        if not self.available or self._rec is None:
            return False

        self._batch.extend(pcm)
        if len(self._batch) < self._batch_bytes:
            return False

        # Предыдущая пачка ещё считается — не копим очередь, а роняем звук.
        # Слово длится куда дольше двухсот миллисекунд, так что попадёт
        # в следующую пачку; отставание же било бы по всей сессии.
        if self._busy:
            self._batch.clear()
            return False

        batch = bytes(self._batch)
        self._batch.clear()
        self._busy = True
        try:
            hit = await asyncio.to_thread(self._process, batch)
        finally:
            self._busy = False

        if hit:
            return True

        # Долго ничего не слышали — освежаем распознаватель, пока он не
        # оглох от накопленного состояния.
        if time.monotonic() - self._last_reset > _IDLE_RESET_S:
            self.reset()
        return False

    def _process(self, pcm: bytes) -> bool:
        try:
            if self._rec.AcceptWaveform(pcm):
                return self._hit(self._rec.Result())
            # PartialResult ловит слово, не дожидаясь конца фразы: человек
            # говорит «Компьютер, включи музыку» слитно, и ждать паузы значит
            # проглотить начало команды.
            return self._hit(self._rec.PartialResult(), partial=True)
        except Exception:
            # Распознаватель может испортиться — поднимаем новый, чтобы
            # колонка не оглохла до перезапуска сервера.
            log.exception("сбой распознавания активационного слова, пересоздаю")
            try:
                self._rec = self._model.new_recognizer()
            except Exception:
                log.exception("не удалось пересоздать распознаватель")
                self.available = False
            return False

    def _hit(self, raw: str, partial: bool = False) -> bool:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return False
        words = data.get("partial" if partial else "text", "").split()
        if _contains_sequence(words, self._wake_parts):
            self.reset()
            return True
        return False


def _contains_sequence(words: list[str], wanted: list[str]) -> bool:
    """Идут ли слова фразы подряд в распознанном тексте."""
    if not wanted or len(words) < len(wanted):
        return False
    for i in range(len(words) - len(wanted) + 1):
        if words[i : i + len(wanted)] == wanted:
            return True
    return False
