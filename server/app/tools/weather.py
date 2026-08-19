"""Погода через Open-Meteo — бесплатно и без ключа."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Коды погоды WMO. Формулировки короткие: их произносит колонка вслух.
_WMO: dict[int, str] = {
    0: "ясно",
    1: "малооблачно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "гроза с градом",
}


_WEEKDAYS = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу",
             "в субботу", "в воскресенье"]


async def get_weather(
    city: str | None,
    default_city: str,
    default_lat: float,
    default_lon: float,
    period: str = "now",
) -> str:
    name = city or default_city
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if city:
                coords = await _geocode(client, city)
                if coords is None:
                    return f"Не нашёл город {city}."
                lat, lon, name = coords
            else:
                lat, lon = default_lat, default_lon
            return await _forecast(client, lat, lon, name, period)
    except httpx.HTTPError as exc:
        log.warning("погода недоступна: %s", exc)
        return "Не удалось получить погоду — нет связи с сервисом."


async def _geocode(client: httpx.AsyncClient, city: str) -> tuple[float, float, str] | None:
    response = await client.get(
        _GEOCODE_URL, params={"name": city, "count": 1, "language": "ru", "format": "json"}
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        return None
    top = results[0]
    return top["latitude"], top["longitude"], top.get("name", city)


async def _forecast(
    client: httpx.AsyncClient, lat: float, lon: float, name: str, period: str
) -> str:
    # Неделя — семь дней, «завтра» — два (сегодня нужно, чтобы добраться
    # до индекса 1), «сейчас» — один.
    days = {"week": 7, "tomorrow": 2}.get(period, 1)
    response = await client.get(
        _FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,"
                     "precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": days,
        },
    )
    response.raise_for_status()
    data = response.json()
    daily = data["daily"]

    if period == "tomorrow":
        return f"{name}, завтра: {_day_line(daily, 1)}."
    if period == "week":
        return _week_line(name, daily)

    current = data["current"]
    temp = round(current["temperature_2m"])
    feels = round(current["apparent_temperature"])
    description = _WMO.get(current["weather_code"], "без осадков")
    low = round(daily["temperature_2m_min"][0])
    high = round(daily["temperature_2m_max"][0])

    parts = [f"{name}: сейчас {temp} градусов, {description}"]
    if abs(feels - temp) >= 3:
        parts.append(f"ощущается как {feels}")
    parts.append(f"днём от {low} до {high}")
    return ", ".join(parts) + "."


def _day_line(daily: dict, i: int) -> str:
    low = round(daily["temperature_2m_min"][i])
    high = round(daily["temperature_2m_max"][i])
    description = _WMO.get(daily["weather_code"][i], "без осадков")
    line = f"{description}, от {low} до {high}"
    rain = (daily.get("precipitation_probability_max") or [None] * (i + 1))[i]
    # Про осадки говорим, только когда они правда вероятны: «вероятность
    # дождя ноль процентов» в каждой фразе — лишний шум в колонке.
    if rain is not None and rain >= 40:
        line += f", вероятность осадков {rain} процентов"
    return line


def _week_line(name: str, daily: dict) -> str:
    import datetime

    parts = []
    for i, date_str in enumerate(daily["time"]):
        if i == 0:
            label = "сегодня"
        elif i == 1:
            label = "завтра"
        else:
            weekday = datetime.date.fromisoformat(date_str).weekday()
            label = _WEEKDAYS[weekday]
        low = round(daily["temperature_2m_min"][i])
        high = round(daily["temperature_2m_max"][i])
        description = _WMO.get(daily["weather_code"][i], "без осадков")
        parts.append(f"{label} {description}, от {low} до {high}")
    return f"{name} на неделю: " + "; ".join(parts) + "."
