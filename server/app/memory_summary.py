"""Сворачивает вытесненную историю разговора в структурированную сводку.

Тот же приём, что у `tools/websearch.py`: отдельный, дешёвый вызов к
OpenAI, не тот, что ведёт сам разговор (тем более что при
`VOICE_PROVIDER=claude` разговор вообще не через OpenAI). Без этого
`ConversationMemory` хранила только скользящее окно последних реплик —
всё, что вытеснялось за него, пропадало насовсем при каждом сохранении,
даже то, что человек явно упоминал раньше и вправе был ожидать, что
колонка помнит.

Сводка — не проза, а поля. Свободный текст однажды уже вышел боком:
в него затесалось «необходимо продолжать или запускать музыку, когда
она вернётся», модель прочитала это как указание и на вопрос о погоде
включила ABBA. Факт, разложенный по полю «предпочтения», указанием не
выглядит. Тот же принцип — у `mem_local_short` в xiaozhi-esp32-server,
только там схема заточена под китайский, а нам нужны сами поля.
"""

from __future__ import annotations

import json
import logging

from app.memory import Turn
from app.openai_client import get as get_openai_client

log = logging.getLogger(__name__)

# Сводка — не диалог в реальном времени, человек её не ждёт молча.
_TIMEOUT_S = 20.0

# Поля сводки. Держим их немногими и очевидными: чем длиннее схема, тем
# охотнее модель начинает выдумывать, чем её заполнить.
FIELDS = ("человек", "предпочтения", "договорённости", "быт")

# Одно поле не должно разрастаться безгранично: сводка идёт в инструкции
# при каждом разговоре, и её объём — это деньги на каждой реплике.
_MAX_ITEMS = 8
_MAX_ITEM_LEN = 160


def _empty() -> dict[str, list[str]]:
    return {f: [] for f in FIELDS}


def normalize(summary) -> dict[str, list[str]]:
    """Приводит что угодно к схеме: строку, старый формат, мусор от модели.

    Файлы, записанные до этой правки, хранят сводку одной строкой —
    читаем их как есть и кладём в «человек», чтобы ничего не потерять.
    """
    if not summary:
        return _empty()

    if isinstance(summary, str):
        text = summary.strip()
        return {**_empty(), "человек": [text[:_MAX_ITEM_LEN]] if text else []}

    if not isinstance(summary, dict):
        return _empty()

    out = _empty()
    for field in FIELDS:
        raw = summary.get(field, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        items: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text and text not in items:
                items.append(text[:_MAX_ITEM_LEN])
        out[field] = items[:_MAX_ITEMS]
    return out


def is_empty(summary) -> bool:
    return not any(normalize(summary).values())


def as_text(summary) -> str:
    """Сводка для инструкций модели — фактами, а не указаниями.

    Заголовок здесь важен не меньше содержимого: без него список фраз
    легко читается как список поручений.
    """
    data = normalize(summary)
    parts = []
    for field in FIELDS:
        if data[field]:
            parts.append(f"{field}: " + "; ".join(data[field]))
    return "\n".join(parts)


_PROMPT = """Ты ведёшь заметку о человеке для домашней колонки.

{prior}Новые реплики разговора:
{lines}

Обнови заметку и верни ТОЛЬКО JSON вида:
{{"человек": [], "предпочтения": [], "договорённости": [], "быт": []}}

Правила:
- «человек» — имя, кто он, кем работает, кто рядом;
- «предпочтения» — что любит и не любит (музыка, еда, темы);
- «договорённости» — что просили запомнить или сделать регулярно;
- «быт» — распорядок, город, привычки.
- Каждый пункт — короткий факт от третьего лица, не обращение и не команда.
  Правильно: «любит ABBA». Неправильно: «включай ABBA когда вернётся».
- Не выдумывай: если в репликах факта нет, поля не заполняй.
- Реплики бывают криво распознаны. Если фраза бессмысленна, на другом
  языке или не связана с остальным разговором — это ошибка распознавания
  речи, а не новый факт. Пропусти её целиком, не пытайся угадать смысл и
  не подставляй вместо непонятного имя реального известного человека —
  этой ошибке подвержены в первую очередь фамилии и профессии.
- В поле «человек» — только то, что человек сказал буквально о себе
  (представился, назвал профессию). Не заполняй его по ассоциации или
  похожести звучания с чьим-то известным именем.
- Не более {max_items} пунктов в поле, каждый до {max_len} символов.
- Сохраняй прежние пункты, если новые реплики их не отменяют."""


async def fold_in(
    api_key: str, model: str, prior_summary, evicted: list[Turn]
) -> dict[str, list[str]]:
    """Возвращает обновлённую сводку. При сбое — прежнюю, без исключений."""
    prior = normalize(prior_summary)
    if not api_key or not evicted:
        return prior

    lines = "\n".join(
        f"{'Пользователь' if t.role == 'user' else 'Колонка'}: {t.text}" for t in evicted
    )
    prior_block = ""
    if any(prior.values()):
        prior_block = (
            "Текущая заметка:\n"
            + json.dumps(prior, ensure_ascii=False, indent=1)
            + "\n\n"
        )

    prompt = _PROMPT.format(
        prior=prior_block, lines=lines, max_items=_MAX_ITEMS, max_len=_MAX_ITEM_LEN
    )

    client = get_openai_client(api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        log.warning("не удалось сжать память разговора: %s", exc)
        return prior

    raw = (response.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("сводка вернулась не JSON — оставляю прежнюю")
        return prior

    updated = normalize(parsed)
    # Пустой ответ модели не должен стирать накопленное.
    return updated if any(updated.values()) else prior
