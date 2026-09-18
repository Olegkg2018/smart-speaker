"""Погода: структурные данные для виджета /satellite и речь для колонки.

Рефакторинг вынес общий разбор ответа Open-Meteo (_parse_current) — здесь
проверяется, что оба потребителя (голосовая фраза get_weather и
структурные данные current_conditions для /api/weather) по-прежнему
получают то, что нужно, и что вынос ничего не сломал в самой фразе.
"""

from __future__ import annotations

import httpx

from app.tools import weather

_FORECAST = {
    "current": {
        "temperature_2m": 5.4,
        "apparent_temperature": 3.0,
        "weather_code": 0,
        "wind_speed_10m": 10.0,
    },
    "daily": {
        "time": ["2026-09-18"],
        "temperature_2m_max": [8.2],
        "temperature_2m_min": [2.1],
        "weather_code": [0],
        "precipitation_probability_max": [10],
    },
}


def _client(handler) -> type[httpx.AsyncClient]:
    """Подменяет сетевой слой httpx на заглушку (см. test_homeassistant.py)."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    httpx.AsyncClient = Patched
    return original


async def test_get_weather_now_phrase_unchanged_after_refactor():
    """Голосовая фраза не должна была измениться при выносе _parse_current."""
    def handler(request):
        return httpx.Response(200, json=_FORECAST)

    original = _client(handler)
    try:
        result = await weather.get_weather(None, "Тестоград", 47.9, 33.3, period="now")
    finally:
        httpx.AsyncClient = original

    assert result == "Тестоград: сейчас 5 градусов, ясно, днём от 2 до 8."


async def test_get_weather_mentions_feels_like_when_it_differs_enough():
    forecast = {
        "current": {
            "temperature_2m": 20.0, "apparent_temperature": 15.0,
            "weather_code": 3, "wind_speed_10m": 5.0,
        },
        "daily": {
            "time": ["2026-09-18"], "temperature_2m_max": [22.0],
            "temperature_2m_min": [14.0], "weather_code": [3],
            "precipitation_probability_max": [0],
        },
    }

    def handler(request):
        return httpx.Response(200, json=forecast)

    original = _client(handler)
    try:
        result = await weather.get_weather(None, "Тестоград", 47.9, 33.3, period="now")
    finally:
        httpx.AsyncClient = original

    assert "ощущается как 15" in result


async def test_current_conditions_returns_structured_fields():
    def handler(request):
        return httpx.Response(200, json=_FORECAST)

    original = _client(handler)
    try:
        result = await weather.current_conditions("Тестоград", 47.9, 33.3)
    finally:
        httpx.AsyncClient = original

    assert result == {
        "temp": 5, "feels": 3, "description": "ясно", "icon": "☀️",
        "low": 2, "high": 8, "city": "Тестоград",
    }


async def test_current_conditions_uses_forecast_days_one():
    """Виджету не нужна недельная сетка — лишний трафик и разбор впустую."""
    captured = {}

    def handler(request):
        captured["forecast_days"] = request.url.params.get("forecast_days")
        return httpx.Response(200, json=_FORECAST)

    original = _client(handler)
    try:
        await weather.current_conditions("Тестоград", 47.9, 33.3)
    finally:
        httpx.AsyncClient = original

    assert captured["forecast_days"] == "1"


async def test_current_conditions_returns_none_on_http_error():
    """Сбой Open-Meteo не должен ронять /api/weather — вызывающий код сам
    решает, показать ли устаревший кэш (см. satellite_page.py)."""
    def handler(request):
        return httpx.Response(500)

    original = _client(handler)
    try:
        result = await weather.current_conditions("Тестоград", 47.9, 33.3)
    finally:
        httpx.AsyncClient = original

    assert result is None
