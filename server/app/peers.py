"""Устройства, подключённые к одной сессии.

Колонка на кухне и телефон в комнате — это не два ассистента, а два
микрофона одного. Поэтому подключений может быть несколько, а разговор,
память и ответ — общие. Слушает тот, кто слышит человека громче;
отвечает вслух тот, у кого есть динамик.

Роли:
  speaker   — есть динамик и экран, обычная колонка (по умолчанию);
  satellite — только микрофон и, может быть, экран.
"""

from __future__ import annotations

import logging
import time

import numpy as np

log = logging.getLogger(__name__)

ROLE_SPEAKER = "speaker"
ROLE_SATELLITE = "satellite"

# За какое время «забывается» громкость источника. Секунда: достаточно,
# чтобы пережить паузу между словами, и мало, чтобы уйдя в другую комнату
# не остаться навсегда «самым громким».
_LEVEL_DECAY_S = 1.0


class Peer:
    """Одно подключённое устройство."""

    def __init__(self, ws, device: str, role: str, has_screen: bool):
        self.ws = ws
        self.device = device
        self.role = role
        self.has_screen = has_screen
        self.codec = None  # выставляется сессией после согласования
        # Скользящая оценка громкости этого микрофона — по ней выбираем,
        # кого слушать.
        self._level = 0.0
        self._level_at = 0.0

    @property
    def has_speaker(self) -> bool:
        return self.role == ROLE_SPEAKER

    def note_audio(self, pcm: bytes) -> float:
        """Запоминает громкость кадра. Возвращает текущую оценку."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size:
            level = float(np.abs(samples.astype(np.int32)).mean())
            # Берём максимум за окно, а не среднее: человек говорит не
            # непрерывно, и по среднему ближний микрофон проигрывал бы
            # дальнему, если тот стоит в шумной комнате.
            self._level = max(level, self.level)
            self._level_at = time.monotonic()
        return self.level

    @property
    def level(self) -> float:
        """Оценка с затуханием: старая громкость сама сходит на нет."""
        if self._level_at == 0.0:
            return 0.0
        age = time.monotonic() - self._level_at
        if age >= _LEVEL_DECAY_S:
            return 0.0
        return self._level * (1.0 - age / _LEVEL_DECAY_S)

    def __repr__(self) -> str:
        return f"<Peer {self.device} {self.role} level={self.level:.0f}>"


class PeerSet:
    """Все устройства сессии и выбор того, кого слушаем."""

    def __init__(self) -> None:
        self._peers: list[Peer] = []
        # Кого слушаем сейчас. Меняется только между репликами: если
        # переключиться посреди фразы, распознавание получит склейку из
        # двух микрофонов и половину слов потеряет.
        self._active: Peer | None = None

    def add(self, peer: Peer) -> None:
        self._peers.append(peer)
        log.info(
            "устройство «%s» подключилось (%s), всего в сессии: %d",
            peer.device, peer.role, len(self._peers),
        )

    def remove(self, peer: Peer) -> None:
        if peer in self._peers:
            self._peers.remove(peer)
        if self._active is peer:
            self._active = None
        log.info("устройство «%s» отключилось, осталось: %d", peer.device, len(self._peers))

    @property
    def empty(self) -> bool:
        return not self._peers

    def all(self) -> list[Peer]:
        return list(self._peers)

    def speakers(self) -> list[Peer]:
        return [p for p in self._peers if p.has_speaker]

    def screens(self) -> list[Peer]:
        return [p for p in self._peers if p.has_screen]

    @property
    def active(self) -> Peer | None:
        return self._active

    def choose_active(self) -> Peer | None:
        """Выбирает микрофон на начало реплики — самый громкий.

        Зовётся один раз, когда сервер начинает слушать. Дальше источник
        не меняется до конца реплики.
        """
        if not self._peers:
            self._active = None
        elif len(self._peers) == 1:
            self._active = self._peers[0]
        else:
            best = max(self._peers, key=lambda p: p.level)
            if self._active is not best:
                log.info(
                    "слушаю «%s» (громкость %.0f против %s)",
                    best.device, best.level,
                    ", ".join(f"{p.device} {p.level:.0f}" for p in self._peers if p is not best),
                )
            self._active = best
        return self._active

    def release_active(self) -> None:
        """Реплика кончилась — следующий раз выбираем заново."""
        self._active = None

    def accepts_audio(self, peer: Peer) -> bool:
        """Брать ли звук этого устройства в распознавание.

        Пока источник не выбран (сервер не слушает) — не берём ни у кого,
        но громкость считаем у всех: по ней и будет сделан выбор.
        """
        return self._active is peer
