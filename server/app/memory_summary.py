"""Сворачивает вытесненную историю разговора в короткую сводку.

Тот же приём, что у `tools/websearch.py`: отдельный, дешёвый вызов к
OpenAI, не тот, что ведёт сам разговор (тем более что при
`VOICE_PROVIDER=claude` разговор вообще не через OpenAI). Без этого
`ConversationMemory` хранила только скользящее окно последних реплик —
всё, что вытеснялось за него, пропадало насовсем при каждом сохранении,
даже то, что человек явно упоминал раньше и вправе был ожидать, что
колонка помнит.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from app.memory import Turn

log = logging.getLogger(__name__)

# Сводка — не диалог в реальном времени, человек её не ждёт молча.
_TIMEOUT_S = 20.0


async def fold_in(api_key: str, model: str, prior_summary: str, evicted: list[Turn]) -> str:
    """Возвращает обновлённую сводку. При сбое — прежнюю, без исключений."""
    if not api_key or not evicted:
        return prior_summary

    lines = "\n".join(
        f"{'Пользователь' if t.role == 'user' else 'Колонка'}: {t.text}" for t in evicted
    )
    prompt = (
        (f"Текущая сводка прошлых разговоров с этим человеком:\n{prior_summary}\n\n"
         if prior_summary else "")
        + "Новые реплики, которые нужно в неё вобрать:\n"
        + lines
        + "\n\nОбнови сводку: несколько предложений на русском, только факты, "
        "предпочтения и договорённости, которые пригодятся в будущих "
        "разговорах (имена, дела, привычки, что просили не забыть). Не "
        "пересказывай реплики подряд — слей всё в единый компактный текст, "
        "как будто пишешь заметку самому себе перед следующим разговором. "
        "Ответь только текстом сводки, без пояснений и заголовков."
    )
    client = AsyncOpenAI(api_key=api_key, timeout=_TIMEOUT_S)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("не удалось сжать память разговора: %s", exc)
        return prior_summary

    text = (response.choices[0].message.content or "").strip()
    return text or prior_summary
