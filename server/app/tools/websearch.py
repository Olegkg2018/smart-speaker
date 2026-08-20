"""Поиск в интернете через встроенный инструмент OpenAI.

Закрывает всё, чего нет в остальных инструментах: курс валют, часы работы
магазина, счёт матча, любой факт, который модель знать не может. Своих лент
и API для этого не напасёшься, а поиск отвечает одной фразой.

Приём подсмотрен в voicepe-realtime: это второй, отдельный вызов к OpenAI —
разговор идёт через Realtime, а поиск делает Responses API, который умеет
ходить в сеть.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

# Поиск идёт в разговоре, где человек ждёт ответа голосом: лучше сказать
# «не нашёл», чем молчать полминуты.
_TIMEOUT_S = 25.0


async def web_search(api_key: str, model: str, query: str) -> str:
    if not api_key:
        return "Поиск не настроен: нет ключа OpenAI."
    query = query.strip()
    if not query:
        return "Не расслышал, что искать."

    client = AsyncOpenAI(api_key=api_key, timeout=_TIMEOUT_S)
    try:
        response = await client.responses.create(
            model=model,
            tools=[{"type": "web_search"}],
            # Ответ пойдёт в синтез речи, поэтому просим сразу пригодный для
            # произнесения текст: без ссылок, списков и разметки.
            input=(
                f"{query}\n\n"
                "Ответь одной-двумя короткими фразами, как в разговоре. "
                "Без ссылок, без списков, без разметки — ответ будет "
                "произнесён вслух. Если точных данных нет, скажи об этом прямо."
            ),
        )
    except Exception as exc:
        log.warning("поиск в интернете не удался: %s", exc)
        return "Не смог найти — интернет не отвечает."

    answer = (response.output_text or "").strip()
    if not answer:
        return "Ничего не нашёл."
    return _strip_citations(answer)


def _strip_citations(text: str) -> str:
    """Убирает ссылки-сноски: вслух они звучат как набор букв.

    Поиск возвращает их в виде ([домен](адрес)) прямо посреди фразы.
    """
    import re

    text = re.sub(r"\(\[[^\]]+\]\([^)]+\)\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return " ".join(text.split())
