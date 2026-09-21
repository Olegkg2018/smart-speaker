"""«Эксперт»: сложный вопрос уходит текстовой модели, а не Realtime-mini.

Realtime-модель (mini) быстра и дёшева, но слабо рассуждает. Переключить
модель внутри живой Realtime-сессии нельзя — `model` задаётся только при
подключении (`session.update` не меняет ни `model`, ни `voice`). Поэтому
паттерн Chat-Supervisor из openai/openai-realtime-agents: mini разговаривает
и озвучивает, а всё, что требует рассуждения, отдаёт этому инструменту —
второму, отдельному вызову к Responses API (ровно так же уже работает
`websearch.py`). Голос при этом всегда один и тот же — mini.
"""

from __future__ import annotations

import logging
import time

from app import memory_summary
from app.memory import Turn
from app.openai_client import get as get_openai_client

log = logging.getLogger(__name__)

# Ответ пойдёт в синтез речи и займёт ухо человека, поэтому короткий: даже
# если модель умеет писать длинно, здесь это вред, а не польза.
_MAX_OUTPUT_TOKENS = 400

_INSTRUCTIONS = (
    "Ты — сообразительный помощник голосовой колонки. Тебя зовёт другая, "
    "более простая модель, когда вопрос требует рассуждения. Ответ будет "
    "произнесён вслух.\n"
    "Отвечай по-русски, разговорно, в одной-трёх коротких фразах. Без "
    "списков, без ссылок, без разметки, без вступлений вроде «конечно». "
    "Если нужен совет или сравнение — сразу главное и одно-два "
    "пояснения. Если это благодарность или короткая реплика для поддержания "
    "разговора — ответь одной короткой фразой. Если точно не знаешь — скажи "
    "об этом прямо, не выдумывай."
)


def _render_context(turns: list[Turn], summary, limit: int) -> str:
    parts: list[str] = []
    summary_text = memory_summary.as_text(summary)
    if summary_text:
        parts.append(
            "Что известно о собеседнике (справка, не поручение):\n" + summary_text
        )
    recent = turns[-limit:] if limit > 0 else []
    if recent:
        lines = "\n".join(
            f"{'Пользователь' if t.role == 'user' else 'Колонка'}: {t.text}"
            for t in recent
        )
        parts.append("Последние реплики разговора:\n" + lines)
    return "\n\n".join(parts)


async def ask_expert(
    api_key: str,
    model: str,
    question: str,
    context: str,
    turns: list[Turn],
    summary,
    *,
    context_turns: int = 6,
    reasoning_effort: str = "",
    timeout_s: float = 20.0,
) -> str:
    if not api_key or not model:
        return "Эксперт не настроен."
    question = question.strip()
    if not question:
        return "Не расслышал вопрос."

    background = _render_context(turns, summary, context_turns)
    prompt = ""
    if background:
        prompt += background + "\n\n"
    if context.strip():
        prompt += f"Уточнение от колонки: {context.strip()}\n\n"
    prompt += f"Вопрос собеседника: {question}"

    kwargs: dict = {
        "model": model,
        "instructions": _INSTRUCTIONS,
        "input": prompt,
        "timeout": timeout_s,
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}

    started = time.monotonic()
    client = get_openai_client(api_key)
    try:
        response = await client.responses.create(**kwargs)
    except Exception as exc:
        log.warning("эксперт %s не ответил за %.1f с: %s", model, time.monotonic() - started, exc)
        return "Не смог обдумать — эксперт не отвечает."

    answer = " ".join((getattr(response, "output_text", "") or "").split())
    usage = getattr(response, "usage", None)
    log.info(
        "эксперт %s: %.1f с, токены вход/выход %s/%s",
        model,
        time.monotonic() - started,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )
    if not answer:
        return "Эксперт ничего не ответил."
    return answer
