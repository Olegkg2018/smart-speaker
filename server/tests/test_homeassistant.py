import httpx
import pytest

from app.tools import homeassistant as ha

_STATES = [
    {
        "entity_id": "sensor.bedroom_temperature",
        "state": "21.5",
        "attributes": {"friendly_name": "Спальня температура", "unit_of_measurement": "°C"},
    },
    {
        "entity_id": "binary_sensor.front_door",
        "state": "off",
        "attributes": {"friendly_name": "Входная дверь"},
    },
    {
        "entity_id": "sensor.broken",
        "state": "unavailable",
        "attributes": {"friendly_name": "Сломанный датчик"},
    },
]


def _client(handler) -> None:
    """Подменяет сетевой слой httpx на заглушку."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    httpx.AsyncClient = Patched
    return original


async def test_sensor_search_by_room(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_STATES)

    original = _client(handler)
    try:
        result = await ha.read_sensors("http://ha", "eyJhbGciOiJIUzI1NiJ9.test", "спальня")
    finally:
        httpx.AsyncClient = original

    assert "21.5" in result and "°C" in result
    assert "дверь" not in result.lower()


async def test_unavailable_sensors_are_skipped(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_STATES)

    original = _client(handler)
    try:
        result = await ha.read_sensors("http://ha", "eyJhbGciOiJIUzI1NiJ9.test", "")
    finally:
        httpx.AsyncClient = original

    # «Сломанный датчик: unavailable» вслух — бесполезный шум.
    assert "Сломанный" not in result


async def test_nothing_found_is_honest():
    def handler(request):
        return httpx.Response(200, json=_STATES)

    original = _client(handler)
    try:
        result = await ha.read_sensors("http://ha", "eyJhbGciOiJIUzI1NiJ9.test", "гараж")
    finally:
        httpx.AsyncClient = original

    assert "не нашёл" in result.lower()


async def test_unreachable_home_says_so():
    def handler(request):
        raise httpx.ConnectError("нет связи")

    original = _client(handler)
    try:
        result = await ha.read_sensors("http://ha", "eyJhbGciOiJIUzI1NiJ9.test", "спальня")
    finally:
        httpx.AsyncClient = original

    assert "не отвечает" in result.lower()


async def test_not_configured():
    assert "не настроен" in (await ha.read_sensors("", "", "спальня")).lower()
    assert "не настроен" in (await ha.ask_home("", "", "включи свет")).lower()


async def test_ask_home_extracts_speech():
    def handler(request):
        return httpx.Response(
            200,
            json={"response": {"speech": {"plain": {"speech": "Включила свет в спальне"}}}},
        )

    original = _client(handler)
    try:
        result = await ha.ask_home("http://ha", "eyJhbGciOiJIUzI1NiJ9.test", "включи свет в спальне")
    finally:
        httpx.AsyncClient = original

    assert result == "Включила свет в спальне"


async def test_ask_home_survives_unknown_shape():
    """Формат ответа отличается между версиями HA — падать нельзя."""

    def handler(request):
        return httpx.Response(200, json={"что-то": "другое"})

    original = _client(handler)
    try:
        result = await ha.ask_home("http://ha", "eyJhbGciOiJIUzI1NiJ9.test", "включи свет")
    finally:
        httpx.AsyncClient = original

    assert "промолчал" in result.lower()


async def test_non_ascii_token_does_not_crash():
    """Токен с кириллицей — опечатка при настройке, а не повод падать."""
    result = await ha.read_sensors("http://ha", "токен-с-кириллицей", "спальня")
    assert "не отвечает" in result.lower()
