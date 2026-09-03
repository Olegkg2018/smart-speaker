"""Веб-страница управления: что запланировано, что записано, что помнит.

Голосом всё это ставится легко, а вот посмотреть и убрать лишнее было
нечем: будильник ставится одной фразой и дальше живёт невидимо, пока не
зазвонит. Плюс сама колонка иногда слышит не то — и тогда в списках и
памяти оседает мусор, который надо чем-то вычищать.

Отдельного фронтенда нет намеренно: одна страница на ванильном HTML,
которую отдаёт тот же FastAPI, что и всё остальное. Ставить сборщик
ради формы с тремя таблицами — дороже, чем польза.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import memory_summary
from app.config import settings
from app.tools import alarms as alarms_tool
from app.tools import lists as lists_tool
from app.tools import notes as notes_tool

router = APIRouter()


# ---------- данные ----------


@router.get("/api/alarms")
async def get_alarms() -> list[dict]:
    return [
        {"id": a.id, "at": a.at, "label": a.label, "sound": a.sound}
        for a in alarms_tool.load(settings.alarms_dir)
    ]


@router.delete("/api/alarms/{alarm_id}")
async def delete_alarm(alarm_id: str) -> dict:
    before = {a.id for a in alarms_tool.load(settings.alarms_dir)}
    if alarm_id not in before:
        raise HTTPException(status_code=404, detail="нет такого будильника")
    alarms_tool.drop(settings.alarms_dir, alarm_id)
    return {"ok": True}


def _list_names(lists_dir: Path) -> list[str]:
    if not lists_dir.is_dir():
        return []
    return sorted(p.stem for p in lists_dir.glob("*.json"))


@router.get("/api/lists")
async def get_lists() -> dict[str, list[str]]:
    return {
        name: lists_tool.load_items(settings.lists_dir, name)
        for name in _list_names(settings.lists_dir)
    }


class ListItem(BaseModel):
    item: str


@router.delete("/api/lists/{name}")
async def delete_list_item(name: str, item: str) -> dict:
    """Убирает одну позицию из списка. Пустой список файлом не оставляем."""
    path = settings.lists_dir / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="нет такого списка")
    items = lists_tool.load_items(settings.lists_dir, name)
    if item not in items:
        raise HTTPException(status_code=404, detail="нет такой позиции")
    items = [i for i in items if i != item]
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@router.get("/api/notes")
async def get_notes() -> list[str]:
    return notes_tool.load(settings.notes_dir)


@router.delete("/api/notes")
async def delete_note(text: str) -> dict:
    kept = [n for n in notes_tool.load(settings.notes_dir) if n != text]
    path = settings.notes_dir / "notes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@router.get("/api/memory")
async def get_memory() -> dict:
    """Что колонка помнит о прошлых разговорах — по каждой колонке отдельно."""
    out: dict[str, dict] = {}
    if settings.memory_dir.is_dir():
        for p in sorted(settings.memory_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out[p.stem] = {
                # Сводка теперь по полям — страница показывает её
                # разобранной, а не одним абзацем.
                "summary": memory_summary.normalize(data.get("summary")),
                "turns": data.get("turns", []),
            }
    return out


@router.delete("/api/memory/{device}")
async def clear_memory(device: str) -> dict:
    """Забыть разговор. Нужно, когда в память попал мусор от ослышки."""
    path = settings.memory_dir / f"{device}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="нет такой колонки")
    path.write_text(
        json.dumps({"turns": [], "summary": {}}, ensure_ascii=False), encoding="utf-8"
    )
    return {"ok": True}


# ---------- страница ----------

_PAGE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<title>Колонка</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --line: #8883; --muted: #8889; }
  body { font: 16px/1.5 system-ui, sans-serif; margin: 0 auto; padding: 16px;
         max-width: 760px; }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  h2 { font-size: 1.05rem; margin: 28px 0 8px; }
  .sub { color: var(--muted); margin: 0 0 8px; font-size: .9rem; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 8px 4px; border-bottom: 1px solid var(--line); vertical-align: top; }
  td.act { width: 1%; white-space: nowrap; text-align: right; }
  button { font: inherit; cursor: pointer; border: 1px solid var(--line);
           background: transparent; color: inherit; border-radius: 6px;
           padding: 3px 10px; }
  button:hover { border-color: currentColor; }
  .empty { color: var(--muted); font-style: italic; padding: 8px 4px; }
  .when { font-variant-numeric: tabular-nums; font-weight: 600; }
  .sum { background: #8881; padding: 10px; border-radius: 8px;
         white-space: pre-wrap; }
</style>
<h1>Колонка</h1>
<p class="sub">Что запланировано и что записано. Удалять — кнопкой справа.</p>

<h2>Будильники</h2>
<div id="alarms"></div>

<h2>Списки</h2>
<div id="lists"></div>

<h2>Заметки</h2>
<p class="sub">Это колонка держит в голове постоянно, в каждом разговоре.</p>
<div id="notes"></div>

<h2>Память разговоров</h2>
<p class="sub">Если колонка отвечает невпопад — обычно сюда попал мусор от ослышки.</p>
<div id="memory"></div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function del(url) {
  const r = await fetch(url, {method: 'DELETE'});
  if (!r.ok) { alert('Не удалось удалить'); return; }
  load();
}

function sumHtml(sum) {
  if (!sum || typeof sum !== 'object') return '';
  const parts = Object.entries(sum).filter(([,v]) => v && v.length);
  if (!parts.length) return '';
  return '<div class="sum">' + parts.map(([k, v]) =>
    '<b>' + esc(k) + ':</b> ' + v.map(esc).join('; ')).join('<br>') + '</div>';
}

function rows(items, render) {
  if (!items.length) return '<p class="empty">пусто</p>';
  return '<table>' + items.map(render).join('') + '</table>';
}

async function load() {
  const [alarms, lists, notes, memory] = await Promise.all(
    ['alarms','lists','notes','memory'].map(p => fetch('/api/'+p).then(r => r.json()))
  );

  $('alarms').innerHTML = rows(alarms, a => `<tr>
    <td><span class="when">${esc(a.at.replace('T',' ').slice(0,16))}</span>
        ${a.label ? ' — ' + esc(a.label) : ''}
        ${a.sound ? ' <span class="sub">(' + esc(a.sound) + ')</span>' : ''}</td>
    <td class="act"><button onclick="del('/api/alarms/${encodeURIComponent(a.id)}')">убрать</button></td>
  </tr>`);

  const names = Object.keys(lists);
  $('lists').innerHTML = names.length ? names.map(n => `<h3>${esc(n)}</h3>` +
    rows(lists[n], i => `<tr><td>${esc(i)}</td><td class="act">
      <button onclick="del('/api/lists/${encodeURIComponent(n)}?item=${encodeURIComponent(i)}')">убрать</button>
    </td></tr>`)).join('') : '<p class="empty">пусто</p>';

  $('notes').innerHTML = rows(notes, n => `<tr><td>${esc(n)}</td><td class="act">
    <button onclick="del('/api/notes?text=${encodeURIComponent(n)}')">убрать</button>
  </td></tr>`);

  $('memory').innerHTML = Object.entries(memory).map(([dev, m]) => `
    <h3>${esc(dev)}</h3>
    ${sumHtml(m.summary)}
    <p class="sub">реплик сохранено: ${m.turns.length}</p>
    <p><button onclick="if(confirm('Забыть разговор с «${esc(dev)}»?')) del('/api/memory/${encodeURIComponent(dev)}')">забыть всё</button></p>
  `).join('') || '<p class="empty">пусто</p>';
}

load();
setInterval(load, 15000);
</script>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE
