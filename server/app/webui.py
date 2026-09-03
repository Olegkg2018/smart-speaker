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

from app import memory_summary, webstyle
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
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🔊</text></svg>">
<style>
__BASE_CSS__
  .sum { background: var(--surface2); padding: 12px 14px; border-radius: var(--radius-sm);
         white-space: pre-wrap; font-size: .9rem; margin-bottom: 12px; }
  .turns { max-height: 340px; overflow-y: auto; border: 1px solid var(--line);
           border-radius: var(--radius-sm); padding: 4px 14px; margin: 10px 0; }
  .turn { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: .92rem; }
  .turn:last-child { border-bottom: 0; }
  .turn b { color: var(--muted); font-weight: 700; }
  .turn.user b { color: var(--accent); }
  .turn.assistant b { color: var(--good); }
  .device-head { display: flex; align-items: center; justify-content: space-between;
                 margin: 18px 0 8px; }
  .device-head:first-child { margin-top: 0; }
  .device-head b { font-size: .95rem; }
</style>

__NAV__
<div class="wrap">

  <div class="hero">
    <h1>Управление колонкой</h1>
    <p>Что запланировано и что записано. Голосом это ставится одной фразой — убрать лишнее можно здесь.</p>
  </div>

  <div class="card">
    <h2>⏰ Будильники</h2>
    <div id="alarms"></div>
  </div>

  <div class="card">
    <h2>📋 Списки</h2>
    <div id="lists"></div>
  </div>

  <div class="card">
    <h2>📝 Заметки</h2>
    <p class="hint">Это колонка держит в голове постоянно, в каждом разговоре.</p>
    <div id="notes"></div>
  </div>

  <div class="card">
    <h2>🧠 Память разговоров</h2>
    <p class="hint">Если колонка отвечает невпопад — обычно сюда попал мусор от ослышки.</p>
    <div id="memory"></div>
  </div>

</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function del(url) {
  const r = await fetch(url, {method: 'DELETE'});
  if (!r.ok) { alert('Не удалось удалить'); return; }
  load();
}

const ROLE_LABEL = {user: 'Вы', assistant: 'Колонка'};

function turnsHtml(turns) {
  if (!turns.length) return '<p class="empty">разговоров ещё не было</p>';
  return '<div class="turns">' + turns.map(t => `<div class="turn ${esc(t.role)}">
    <b>${esc(ROLE_LABEL[t.role] || t.role)}:</b> ${esc(t.text)}</div>`).join('') + '</div>';
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
    <td class="act"><button class="danger" onclick="del('/api/alarms/${encodeURIComponent(a.id)}')">убрать</button></td>
  </tr>`);

  const names = Object.keys(lists);
  $('lists').innerHTML = names.length ? names.map(n => `<h3>${esc(n)}</h3>` +
    rows(lists[n], i => `<tr><td>${esc(i)}</td><td class="act">
      <button class="danger" onclick="del('/api/lists/${encodeURIComponent(n)}?item=${encodeURIComponent(i)}')">убрать</button>
    </td></tr>`)).join('') : '<p class="empty">пусто</p>';

  $('notes').innerHTML = rows(notes, n => `<tr><td>${esc(n)}</td><td class="act">
    <button class="danger" onclick="del('/api/notes?text=${encodeURIComponent(n)}')">убрать</button>
  </td></tr>`);

  $('memory').innerHTML = Object.entries(memory).map(([dev, m]) => `
    <div class="device-head"><b>${esc(dev)}</b><span class="badge">реплик: ${m.turns.length}</span></div>
    ${sumHtml(m.summary)}
    ${turnsHtml(m.turns)}
    <p><button class="danger" onclick="if(confirm('Забыть разговор с «${esc(dev)}»?')) del('/api/memory/${encodeURIComponent(dev)}')">забыть всё</button></p>
  `).join('') || '<p class="empty">пусто</p>';
}

load();
setInterval(load, 15000);
</script>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE.replace("__BASE_CSS__", webstyle.BASE_CSS).replace("__NAV__", webstyle.nav("home"))
