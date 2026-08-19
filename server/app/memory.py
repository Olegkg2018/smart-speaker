"""Текст разговора на диске — переживает обрыв связи и перезапуск сервера.

Ключ — имя колонки из "hello", не привязано к конкретному человеку. Хранит
только текст реплик, без аудио, без шифрования (домашний сервер). Скользящее
окно того же порядка, что и раньше жило только в памяти процесса — разница
в том, что теперь оно не исчезает при переподключении Wi-Fi или пересборке
контейнера.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


class ConversationMemory:
    def __init__(self, storage_dir: Path, device: str, limit: int):
        self._path = storage_dir / f"{device}.json"
        self._limit = limit
        self._turns: list[Turn] = self._load()

    def _load(self) -> list[Turn]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [Turn(role=t["role"], text=t["text"]) for t in data][-self._limit :]
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
            log.warning("не удалось прочитать память «%s»: %s", self._path, exc)
            return []

    @property
    def turns(self) -> list[Turn]:
        return list(self._turns)

    def append(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._turns.append(Turn(role, text))
        self._turns = self._turns[-self._limit :]
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([asdict(t) for t in self._turns], ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)  # атомарно — сбой на записи не оставит битый файл
        except OSError as exc:
            log.warning("не удалось сохранить память «%s»: %s", self._path, exc)
