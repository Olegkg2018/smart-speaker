"""Описания инструментов для модели и диспетчер вызовов.

Описания намеренно подробные и говорят, *когда* инструмент вызывать, а не
только что он делает: на этом заметно растёт доля правильных вызовов.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools import music, news, timers, weather
from app.tools.context import ToolContext

log = logging.getLogger(__name__)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "play_music",
        "description": (
            "Включить музыку. Вызывай сразу, как только пользователь просит что-то "
            "поставить, включить или послушать — не переспрашивай уточнений, если "
            "запрос хоть как-то понятен. Ищет сначала в домашней фонотеке, затем "
            "в интернете, так что играть можно что угодно, а не только то, что "
            "есть дома."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Исполнитель, название трека, альбом или жанр. "
                        "Например: «Кино Группа крови», «спокойный джаз»."
                    ),
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "control_playback",
        "description": (
            "Управлять текущим воспроизведением: пауза, продолжить, выключить. "
            "Вызывай на фразы вроде «стоп», «выключи музыку», «поставь на паузу»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "stop"],
                    "description": "Что сделать с воспроизведением.",
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_volume",
        "description": (
            "Задать громкость колонки. Вызывай на «сделай громче», «потише», "
            "«громкость на половину». Оценивай новое значение относительно текущего."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "number",
                    "description": "Громкость от 0.0 (тишина) до 1.0 (максимум).",
                }
            },
            "required": ["level"],
        },
    },
    {
        "name": "get_weather",
        "description": (
            "Погода: сейчас, на завтра или на неделю вперёд. Вызывай на любой "
            "вопрос о погоде, температуре, дожде, о том, что надеть, и на "
            "«что будет завтра» — прогноз тоже здесь, отдельного инструмента нет."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Город. Не указывай, если спрашивают про здесь и сейчас.",
                },
                "period": {
                    "type": "string",
                    "enum": ["now", "tomorrow", "week"],
                    "description": (
                        "now — что за окном сейчас (по умолчанию); "
                        "tomorrow — прогноз на завтра; "
                        "week — на ближайшую неделю."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_news",
        "description": (
            "Свежие заголовки новостей из лент СМИ, прямо сейчас. Это "
            "единственный способ узнать, что происходит в мире: собственные "
            "сведения модели устарели. Вызывай на «что нового», «новости», "
            "«что в мире», «что слышно», «расскажи что происходит» — сразу, "
            "не переспрашивая и не предупреждая, что данные могут быть "
            "неактуальны."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Тема для фильтра, например «спорт». Обычно не нужна.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "set_timer",
        "description": (
            "Поставить таймер. По истечении колонка сама объявит об этом вслух. "
            "Вызывай на «поставь таймер», «разбуди через», «напомни через»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "Через сколько секунд сработать.",
                },
                "label": {
                    "type": "string",
                    "description": "На что таймер, например «чайник». Необязательно.",
                },
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "cancel_timers",
        "description": "Отменить все активные таймеры.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    """Выполняет инструмент. Возвращает короткую строку для модели.

    Ответы намеренно короткие: контекст голосового диалога и так небольшой,
    а модель всё равно перескажет результат своими словами.
    """
    log.info("инструмент %s(%s)", name, args)
    try:
        match name:
            case "play_music":
                return await music.play_music(ctx, args["query"])
            case "control_playback":
                return await music.control_playback(ctx, args["action"])
            case "set_volume":
                return await music.set_volume(ctx, float(args["level"]))
            case "get_weather":
                return await weather.get_weather(
                    args.get("city"),
                    ctx.settings.default_city,
                    ctx.settings.default_latitude,
                    ctx.settings.default_longitude,
                    args.get("period", "now"),
                )
            case "get_news":
                return await news.get_news(ctx.settings.news_feeds, args.get("topic"))
            case "set_timer":
                return await timers.set_timer(ctx, int(args["seconds"]), args.get("label"))
            case "cancel_timers":
                return await timers.cancel_timers(ctx)
            case _:
                return f"Неизвестный инструмент: {name}"
    except Exception as exc:
        # Модель должна узнать об ошибке и попробовать иначе, а не молча зависнуть.
        log.exception("инструмент %s упал", name)
        return f"Ошибка при выполнении: {exc}"
