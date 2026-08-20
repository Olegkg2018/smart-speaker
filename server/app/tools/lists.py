"""Списки покупок, дел и всего остального, что диктуют голосом.

Хранятся обычными файлами рядом с памятью разговоров: списки короткие, их
читают глазами и правят руками, а база данных ради двух десятков строк
только мешала бы. Папка вынесена из Docker, поэтому переживает пересборку.

Списки заводятся сами при первом упоминании: человек говорит «добавь молоко
в покупки», а не «создай список покупок, затем добавь в него молоко».
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Сколько пунктов зачитывать вслух за раз. Длинный список в колонке
# невыносим: к десятому пункту забываешь первый.
_READ_ALOUD = 12

# Имя списка идёт прямо в имя файла, поэтому всё лишнее вычищаем: голосом
# легко надиктовать что угодно, вплоть до пути с двумя точками.
_SAFE_NAME_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def _list_path(lists_dir: Path, name: str) -> Path:
    safe = _SAFE_NAME_RE.sub("", name.strip().lower()).strip()
    safe = re.sub(r"\s+", "-", safe) or "список"
    return lists_dir / f"{safe}.json"


def _load(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(item) for item in data if str(item).strip()]
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        log.warning("не удалось прочитать список «%s»: %s", path.name, exc)
        return []


def _save(path: Path, items: list[str]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)  # атомарно: сбой на записи не оставит битый файл
        return True
    except OSError as exc:
        log.warning("не удалось сохранить список «%s»: %s", path.name, exc)
        return False


def _speak_items(items: list[str]) -> str:
    shown = items[:_READ_ALOUD]
    body = ", ".join(shown)
    if len(items) > len(shown):
        return f"{body} и ещё {len(items) - len(shown)}"
    return body


def load_items(lists_dir: Path, list_name: str) -> list[str]:
    """Содержимое списка как есть. Нужно тем, кто не зачитывает его вслух —
    например плейлисту, который проигрывает пункты по очереди."""
    return _load(_list_path(lists_dir, list_name))


async def add_to_list(lists_dir: Path, list_name: str, item: str) -> str:
    item = item.strip()
    if not item:
        return "Не расслышал, что добавить."

    path = _list_path(lists_dir, list_name)
    items = _load(path)

    # Повтор — обычное дело: человек не помнит, что уже диктовал. Молча
    # дублировать хуже, чем сказать об этом.
    if any(item.lower() == existing.lower() for existing in items):
        return f"{item} уже в списке «{list_name}»."

    items.append(item)
    if not _save(path, items):
        return "Не смог сохранить список."
    return f"Добавил {item}. Всего в списке «{list_name}»: {len(items)}."


async def read_list(lists_dir: Path, list_name: str) -> str:
    items = _load(_list_path(lists_dir, list_name))
    if not items:
        return f"Список «{list_name}» пуст."
    return f"В списке «{list_name}»: {_speak_items(items)}."


async def remove_from_list(lists_dir: Path, list_name: str, item: str) -> str:
    path = _list_path(lists_dir, list_name)
    items = _load(path)
    if not items:
        return f"Список «{list_name}» и так пуст."

    needle = item.strip().lower()
    # Точное совпадение важнее частичного: «молоко» не должно убирать
    # «молоко кокосовое», если первое в списке тоже есть.
    for i, existing in enumerate(items):
        if existing.lower() == needle:
            removed = items.pop(i)
            break
    else:
        for i, existing in enumerate(items):
            if needle in existing.lower():
                removed = items.pop(i)
                break
        else:
            return f"Не нашёл {item} в списке «{list_name}»."

    if not _save(path, items):
        return "Не смог сохранить список."
    left = f"Осталось {len(items)}." if items else "Список теперь пуст."
    return f"Убрал {removed}. {left}"


async def clear_list(lists_dir: Path, list_name: str) -> str:
    path = _list_path(lists_dir, list_name)
    if not _load(path):
        return f"Список «{list_name}» и так пуст."
    if not _save(path, []):
        return "Не смог очистить список."
    return f"Очистил список «{list_name}»."


async def which_lists(lists_dir: Path) -> str:
    if not lists_dir.is_dir():
        return "Списков пока нет."
    names = sorted(p.stem.replace("-", " ") for p in lists_dir.glob("*.json"))
    if not names:
        return "Списков пока нет."
    return "Есть списки: " + ", ".join(names) + "."
