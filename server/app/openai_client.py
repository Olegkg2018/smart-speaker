"""Общий кэш клиентов AsyncOpenAI по ключу.

`AsyncOpenAI()` сам по себе синхронный и небыстрый — на этой плате ~135 мс
только на конструктор (httpx поднимает SSL-контекст, парсит бандл
сертификатов), и это блокирует цикл событий целиком, если создавать
клиента заново на каждый вызов. `web_search()` и `fold_in()` так и делали —
отсюда заикание разговора ровно в те моменты, когда где-то в фоне
понадобилось слазить в облако (поиск, сворачивание памяти).
"""

from __future__ import annotations

import functools

from openai import AsyncOpenAI


@functools.lru_cache(maxsize=4)
def get(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key)
