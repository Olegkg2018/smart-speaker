"""Умный дом через Home Assistant.

Два разных пути, потому что задачи разные.

Сенсоры читаются напрямую из состояний: это доли секунды, точное значение
и никаких лишних токенов. «Какая температура в спальне» не должно ходить
через ещё одну языковую модель.

Всё остальное — свет, розетки, сцены, сложные фразы — отдаётся встроенному
ассистенту Home Assistant. Он уже знает все устройства дома, их имена и
синонимы; повторять этот справочник у себя значит поддерживать его вручную
и расходиться с реальностью после каждого нового выключателя.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

# Дом отвечает по локальной сети: если за это время не ответил, что-то
# сломалось, и лучше сказать об этом, чем держать человека в тишине.
_TIMEOUT_S = 8.0

# Сколько сущностей показывать, когда человек спрашивает «что дома есть».
_MAX_LISTED = 25


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def read_sensors(base_url: str, token: str, query: str) -> str:
    """Ищет сенсоры по названию и зачитывает их значения."""
    if not base_url or not token:
        return "Умный дом не настроен."

    needle = query.strip().lower()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{base_url}/api/states", headers=_headers(token))
            response.raise_for_status()
            states = response.json()
    except (httpx.HTTPError, UnicodeEncodeError) as exc:
        log.warning("Home Assistant не ответил: %s", exc)
        return "Дом не отвечает."

    matches = []
    for item in states:
        entity_id = item.get("entity_id", "")
        name = (item.get("attributes", {}) or {}).get("friendly_name", "") or entity_id
        if needle and needle not in name.lower() and needle not in entity_id.lower():
            continue
        state = item.get("state", "")
        if state in ("unavailable", "unknown", ""):
            continue
        unit = (item.get("attributes", {}) or {}).get("unit_of_measurement", "")
        matches.append(f"{name}: {state}{(' ' + unit) if unit else ''}")

    if not matches:
        return f"Не нашёл ничего похожего на «{query}»."
    if len(matches) > _MAX_LISTED:
        shown = matches[:_MAX_LISTED]
        return "; ".join(shown) + f" и ещё {len(matches) - len(shown)}."
    return "; ".join(matches) + "."


async def ask_home(base_url: str, token: str, phrase: str, language: str = "ru") -> str:
    """Передаёт фразу встроенному ассистенту Home Assistant.

    Он знает устройства дома по именам, которые вы им дали, включая
    синонимы и комнаты — поэтому сложные команды идут сюда, а не
    разбираются здесь по кусочкам.
    """
    if not base_url or not token:
        return "Умный дом не настроен."

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(
                f"{base_url}/api/conversation/process",
                headers=_headers(token),
                json={"text": phrase, "language": language},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, UnicodeEncodeError) as exc:
        log.warning("ассистент дома не ответил: %s", exc)
        return "Дом не отвечает."

    # Ответ лежит глубоко, и на разных версиях по-разному: вытаскиваем
    # осторожно, чтобы не падать на незнакомой структуре.
    try:
        speech = data["response"]["speech"]["plain"]["speech"]
    except (KeyError, TypeError):
        speech = ""
    return speech.strip() or "Дом промолчал."
