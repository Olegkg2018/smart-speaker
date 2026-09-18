"""Страница /satellite: рендер и ссылка на страницу управления.

Общий вид (webstyle.BASE_CSS/nav) подставляется через .replace() наравне с
__TLS_PORT__ — здесь проверяется, что все три плейсхолдера действительно
заменяются, а не остаются в HTML как текст.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import satellite_page


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(satellite_page.router)
    return TestClient(app)


def test_page_renders_with_no_leftover_placeholders():
    html = _client().get("/satellite").text
    assert "__TLS_PORT__" not in html
    assert "__BASE_CSS__" not in html
    assert "__NAV__" not in html


def test_page_links_back_to_management_page():
    html = _client().get("/satellite").text
    assert '<a class="here" href="/satellite">' in html
    assert 'href="/"' in html


def test_page_has_a_volume_control_for_the_speaker():
    html = _client().get("/satellite").text
    assert 'id="volSlider"' in html
    assert "t: 'volume'" in html or "t:'volume'" in html


# ---------- киоск: часы и погода ----------


def test_page_has_clock_and_weather_widgets():
    html = _client().get("/satellite").text
    assert 'id="clock"' in html
    assert 'id="weather"' in html


def test_weather_endpoint_returns_current_conditions(monkeypatch):
    async def fake_current_conditions(city, lat, lon):
        return {
            "temp": 12, "feels": 10, "description": "малооблачно", "icon": "🌤️",
            "low": 8, "high": 15, "city": city,
        }

    monkeypatch.setattr(satellite_page, "current_conditions", fake_current_conditions)
    # Кэш модульный — сбрасываем, иначе прошлый тест мог его уже заполнить.
    monkeypatch.setattr(satellite_page, "_weather_cache", None)
    monkeypatch.setattr(satellite_page, "_weather_cache_at", 0.0)

    data = _client().get("/api/weather").json()

    assert data["temp"] == 12
    assert data["icon"] == "🌤️"
    assert data["description"] == "малооблачно"


def test_weather_endpoint_survives_open_meteo_being_down(monkeypatch):
    """Сбой источника не должен уронить эндпоинт — виджету достаётся
    пустой ответ (без эталона он просто ничего не покажет)."""
    async def failing_current_conditions(city, lat, lon):
        return None

    monkeypatch.setattr(satellite_page, "current_conditions", failing_current_conditions)
    monkeypatch.setattr(satellite_page, "_weather_cache", None)
    monkeypatch.setattr(satellite_page, "_weather_cache_at", 0.0)

    resp = _client().get("/api/weather")

    assert resp.status_code == 200
    assert resp.json() == {}


def test_weather_endpoint_reuses_cache_within_ttl(monkeypatch):
    calls = []

    async def counting_current_conditions(city, lat, lon):
        calls.append(1)
        return {"temp": 1, "feels": 1, "description": "ясно", "icon": "☀️",
                "low": 0, "high": 2, "city": city}

    monkeypatch.setattr(satellite_page, "current_conditions", counting_current_conditions)
    monkeypatch.setattr(satellite_page, "_weather_cache", None)
    monkeypatch.setattr(satellite_page, "_weather_cache_at", 0.0)

    client = _client()
    client.get("/api/weather")
    client.get("/api/weather")

    assert len(calls) == 1, "второй запрос в пределах TTL не должен дёргать Open-Meteo снова"

