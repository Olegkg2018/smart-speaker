"""Веб-страница управления: показать запланированное и убрать лишнее.

Голосом будильник ставится одной фразой и дальше живёт невидимо, пока не
зазвонит, — посмотреть и отменить было нечем. Здесь проверяется, что
страница честно показывает данные и что удаление трогает ровно одну
запись, а не сносит соседние.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import webui
from app.tools import alarms as alarms_tool


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("alarms_dir", "lists_dir", "notes_dir", "memory_dir"):
        d = tmp_path / name
        d.mkdir()
        monkeypatch.setattr(webui.settings, name, d)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(webui.router)
    return TestClient(app)


def test_page_opens(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Будильники" in r.text


def test_page_links_to_satellite_and_has_no_leftover_placeholders(client):
    """Общий вид (webstyle) подставляется через .replace() — если имя
    плейсхолдера когда-нибудь разъедется между файлами, он останется в
    HTML как есть и будет виден пользователю."""
    html = client.get("/").text
    assert "__BASE_CSS__" not in html
    assert "__NAV__" not in html
    assert '<a class="here" href="/">' in html
    assert 'href="/satellite"' in html


def test_empty_sections_do_not_break(client):
    for path in ("/api/alarms", "/api/notes"):
        assert client.get(path).json() == []
    for path in ("/api/lists", "/api/memory"):
        assert client.get(path).json() == {}


def test_alarm_is_listed_and_can_be_removed(client):
    alarms_tool.save(
        webui.settings.alarms_dir,
        [
            alarms_tool.Alarm(id="a1", at="2026-09-04T05:30:00", label="на работу"),
            alarms_tool.Alarm(id="a2", at="2026-09-04T07:00:00", label="зарядка"),
        ],
    )

    listed = client.get("/api/alarms").json()
    assert [a["id"] for a in listed] == ["a1", "a2"]
    assert listed[0]["label"] == "на работу"

    assert client.delete("/api/alarms/a1").status_code == 200

    left = client.get("/api/alarms").json()
    assert [a["id"] for a in left] == ["a2"], "удалили не тот будильник"


def test_removing_unknown_alarm_is_reported(client):
    assert client.delete("/api/alarms/нет-такого").status_code == 404


def test_list_item_removal_keeps_the_rest(client):
    (webui.settings.lists_dir / "покупки.json").write_text(
        json.dumps(["хлеб", "молоко", "сыр"], ensure_ascii=False), encoding="utf-8"
    )

    assert client.get("/api/lists").json() == {"покупки": ["хлеб", "молоко", "сыр"]}

    r = client.delete("/api/lists/покупки", params={"item": "молоко"})
    assert r.status_code == 200
    assert client.get("/api/lists").json() == {"покупки": ["хлеб", "сыр"]}


def test_note_removal_touches_only_that_note(client):
    (webui.settings.notes_dir / "notes.json").write_text(
        json.dumps(["меня зовут Вика", "мусор от ослышки"], ensure_ascii=False),
        encoding="utf-8",
    )

    client.delete("/api/notes", params={"text": "мусор от ослышки"})
    assert client.get("/api/notes").json() == ["меня зовут Вика"]


def test_memory_is_shown_and_can_be_forgotten(client):
    (webui.settings.memory_dir / "kitchen.json").write_text(
        json.dumps(
            {
                "summary": {"предпочтения": ["любит ABBA"]},
                "turns": [{"role": "user", "text": "привет"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    shown = client.get("/api/memory").json()
    assert shown["kitchen"]["summary"]["предпочтения"] == ["любит ABBA"]
    assert len(shown["kitchen"]["turns"]) == 1

    assert client.delete("/api/memory/kitchen").status_code == 200

    after = client.get("/api/memory").json()
    assert after["kitchen"]["turns"] == []
    assert not any(after["kitchen"]["summary"].values())


def test_page_renders_the_conversation_itself_not_just_a_count(client):
    """Раньше страница показывала только «реплик сохранено: N» — сам
    разговор был виден только через API. Рендер на JS, поэтому здесь можно
    проверить только то, что код для этого в странице есть."""
    html = client.get("/").text
    assert "turnsHtml" in html
    assert "ROLE_LABEL" in html
