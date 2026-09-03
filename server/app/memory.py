"""Текст разговора на диске — переживает обрыв связи и перезапуск сервера.

Ключ — имя колонки из "hello", не привязано к конкретному человеку. Хранит
только текст реплик, без аудио, без шифрования (домашний сервер). Скользящее
окно того же порядка, что и раньше жило только в памяти процесса — разница
в том, что теперь оно не исчезает при переподключении Wi-Fi или пересборке
контейнера.

Окно всё равно конечное: реплики старше limit вытесняются. Раньше они просто
пропадали — append() возвращал ничего, и вызывающий код о вытеснении даже не
узнавал. Теперь append() отдаёт вытесненное вызывающему, а тот (session.py)
сворачивает это в сводку отдельной моделью (app/memory_summary.py) и кладёт
через set_summary() — сюда эта логика не тащится: memory.py не должен знать
про OpenAI, чтобы им пользовался любой голосовой бэкенд одинаково.
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
        self._turns, self._summary = self._load()

    def _load(self) -> tuple[list[Turn], str]:
        if not self._path.exists():
            return [], {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("не удалось прочитать память «%s»: %s", self._path, exc)
            return [], {}

        # Старый формат файла — голый список реплик, без сводки. Файлы,
        # записанные до этой правки, читать нужно как и раньше.
        if isinstance(raw, list):
            turns_data, summary = raw, {}
        else:
            turns_data, summary = raw.get("turns", []), raw.get("summary", {})

        try:
            turns = [Turn(role=t["role"], text=t["text"]) for t in turns_data][-self._limit :]
        except (KeyError, TypeError) as exc:
            log.warning("не удалось прочитать память «%s»: %s", self._path, exc)
            return [], {}
        return turns, summary

    @property
    def turns(self) -> list[Turn]:
        return list(self._turns)

    @property
    def summary(self):
        """Сводка прошлых разговоров — поля, а не проза.

        Старые файлы хранят её строкой; приводит к схеме memory_summary,
        сюда эта логика не тащится — memory.py не знает про модели.
        """
        return self._summary

    def append(self, role: str, text: str) -> list[Turn]:
        """Добавляет реплику. Возвращает вытесненные — их надо свернуть в сводку."""
        text = text.strip()
        if not text:
            return []
        self._turns.append(Turn(role, text))
        evicted: list[Turn] = []
        if len(self._turns) > self._limit:
            overflow = len(self._turns) - self._limit
            evicted, self._turns = self._turns[:overflow], self._turns[overflow:]
        self._save()
        return evicted

    def set_summary(self, summary) -> None:
        self._summary = summary
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            payload = {"turns": [asdict(t) for t in self._turns], "summary": self._summary}
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)  # атомарно — сбой на записи не оставит битый файл
        except OSError as exc:
            log.warning("не удалось сохранить память «%s»: %s", self._path, exc)
