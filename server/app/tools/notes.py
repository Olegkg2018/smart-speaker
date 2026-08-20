"""Постоянные заметки: то, что колонка должна помнить всегда.

Это не история разговора. История — скользящее окно последних реплик, она
неизбежно вытесняется новыми. А сюда попадает сказанное «запомни»: имена
домашних, предпочтения, привычки. Такое должно пережить и вытеснение, и
перезапуск, поэтому лежит на диске и подставляется в инструкции при каждом
разговоре.

Приём подсмотрен в voicepe-realtime: заметки становятся частью инструкций,
а не очередной репликой, иначе они теряются вместе с историей.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Заметки уходят в инструкции при каждом разговоре, а инструкции стоят
# входных токенов. Сотня коротких фраз — это ещё разумно, тысяча — уже нет.
_MAX_NOTES = 100
_MAX_NOTE_CHARS = 200


def _path(notes_dir: Path) -> Path:
    return notes_dir / "notes.json"


def load(notes_dir: Path) -> list[str]:
    path = _path(notes_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(n) for n in data if str(n).strip()]
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        log.warning("не удалось прочитать заметки: %s", exc)
        return []


def _save(notes_dir: Path, notes: list[str]) -> bool:
    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
        tmp = _path(notes_dir).with_suffix(".tmp")
        tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_path(notes_dir))
        return True
    except OSError as exc:
        log.warning("не удалось сохранить заметки: %s", exc)
        return False


async def remember(notes_dir: Path, note: str) -> str:
    note = " ".join(note.split())[:_MAX_NOTE_CHARS]
    if not note:
        return "Не расслышал, что запомнить."

    notes = load(notes_dir)
    if any(note.lower() == existing.lower() for existing in notes):
        return "Это я уже помню."
    if len(notes) >= _MAX_NOTES:
        return "Слишком много заметок — сначала попроси что-нибудь забыть."

    notes.append(note)
    if not _save(notes_dir, notes):
        return "Не смог запомнить."
    return "Запомнил."


async def forget(notes_dir: Path, matching: str) -> str:
    needle = matching.strip().lower()
    if not needle:
        return "Не понял, что забыть."

    notes = load(notes_dir)
    keep = [n for n in notes if needle not in n.lower()]
    removed = len(notes) - len(keep)
    if not removed:
        return "Такого я и не помнил."
    if not _save(notes_dir, keep):
        return "Не смог забыть."
    return "Забыл." if removed == 1 else f"Забыл {removed} записи."


async def list_notes(notes_dir: Path) -> str:
    notes = load(notes_dir)
    if not notes:
        return "Я пока ничего не запоминал."
    return "Вот что я помню: " + "; ".join(notes) + "."


def as_instructions(notes_dir: Path) -> str:
    """Заметки в виде куска инструкций для модели."""
    notes = load(notes_dir)
    if not notes:
        return ""
    lines = "\n".join(f"- {n}" for n in notes)
    return (
        "\n\nЧто тебя просили запомнить об этом доме. Учитывай это в ответах, "
        "но не перечисляй вслух без просьбы:\n" + lines
    )
