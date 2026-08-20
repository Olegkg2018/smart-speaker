"""Описания инструментов для модели и диспетчер вызовов.

Описания намеренно подробные и говорят, *когда* инструмент вызывать, а не
только что он делает: на этом заметно растёт доля правильных вызовов.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools import lists, music, news, timers, weather
from app.tools.context import ToolContext

log = logging.getLogger(__name__)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "play_music",
        "description": (
            "Включить музыку. Вызывай сразу, как только человек просит что-то "
            "поставить, включить или послушать — не переспрашивай, если запрос "
            "хоть как-то понятен. Ищет сначала в домашней фонотеке, потом в "
            "интернете, так что играть можно что угодно."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Исполнитель и название трека — то, что реально "
                        "написано на обложке.\n"
                        "Поиск ищет буквально по словам, а не по смыслу, "
                        "поэтому настроение и пожелания сюда передавать "
                        "нельзя: по запросу «что-нибудь бодрое» находится "
                        "песня со словом «что-нибудь» в названии, а не бодрая "
                        "музыка. Переводи пожелание в конкретику сам: "
                        "«что-нибудь бодрое» → «Queen Don't Stop Me Now», "
                        "«поставь расслабиться» → «lounge chillout», "
                        "«что-то из детства» → исполнитель тех лет. "
                        "Выбирай сам и не спрашивай разрешения."
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
            "Свежие заголовки новостей из украинских лент и Telegram-каналов, "
            "прямо сейчас. Это единственный способ узнать, что происходит: "
            "собственные сведения модели устарели. Вызывай на «что нового», "
            "«новости», «что в мире», «что слышно» — сразу, не переспрашивая "
            "и не предупреждая, что данные могут быть неактуальны.\n"
            "Заголовки приходят как есть, часто по-украински. Зачитывай их "
            "близко к тексту и на том языке, на котором они написаны: это "
            "цитаты новостей, а не твои слова. Переводить, пересказывать "
            "своими словами и добавлять оценки не нужно — так теряется суть. "
            "Можно только убрать значки и сокращения, которые вслух звучат "
            "нелепо, и связать заголовки словами вроде «дальше»."
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
    {
        "name": "add_to_list",
        "description": (
            "Добавить пункт в список: покупки, дела, идеи — любой. Вызывай на "
            "«добавь молоко в покупки», «запиши в список дел позвонить маме», "
            "«не забыть купить хлеб». Список создаётся сам, спрашивать "
            "разрешения не нужно. Если человек не назвал список, а речь про "
            "продукты — это «покупки»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {
                    "type": "string",
                    "description": "Название списка: покупки, дела, идеи.",
                },
                "item": {
                    "type": "string",
                    "description": "Что добавить, как сказал человек: «молоко», «позвонить маме».",
                },
            },
            "required": ["list_name", "item"],
        },
    },
    {
        "name": "read_list",
        "description": (
            "Прочитать список вслух. Вызывай на «что в списке покупок», "
            "«что мне нужно купить», «какие у меня дела». Не пересказывай "
            "результат — он уже готов к произнесению."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {"type": "string", "description": "Название списка."}
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "remove_from_list",
        "description": (
            "Убрать один пункт из списка. Вызывай на «убери молоко из "
            "покупок», «вычеркни хлеб», «купил молоко»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {"type": "string", "description": "Название списка."},
                "item": {"type": "string", "description": "Что убрать."},
            },
            "required": ["list_name", "item"],
        },
    },
    {
        "name": "clear_list",
        "description": (
            "Очистить список целиком. Вызывай на «очисти список покупок», "
            "«всё купил». В отличие от remove_from_list убирает сразу всё."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {"type": "string", "description": "Название списка."}
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "add_current_to_playlist",
        "description": (
            "Добавить играющую сейчас песню в плейлист. Вызывай на «добавь "
            "эту песню в мой плейлист», «сохрани этот трек», «мне нравится, "
            "запомни». Название песни знать не нужно — колонка знает, что "
            "играет. Если плейлист не назвали, используй «мой плейлист»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playlist": {
                    "type": "string",
                    "description": "Название плейлиста, например «мой плейлист».",
                }
            },
            "required": ["playlist"],
        },
    },
    {
        "name": "play_playlist",
        "description": (
            "Включить плейлист целиком: первая песня заиграет сразу, "
            "остальные пойдут следом сами. Вызывай на «включи мой плейлист», "
            "«поставь мою музыку»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playlist": {"type": "string", "description": "Название плейлиста."}
            },
            "required": ["playlist"],
        },
    },
    {
        "name": "which_lists",
        "description": (
            "Перечислить, какие списки вообще заведены. Вызывай на «какие у "
            "меня списки», «что я записывал»."
        ),
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
                return await news.get_news(
                    ctx.settings.news_feeds,
                    args.get("topic"),
                    ctx.settings.news_telegram_channels,
                )
            case "set_timer":
                return await timers.set_timer(ctx, int(args["seconds"]), args.get("label"))
            case "cancel_timers":
                return await timers.cancel_timers(ctx)
            case "add_to_list":
                return await lists.add_to_list(
                    ctx.settings.lists_dir, args["list_name"], args["item"]
                )
            case "read_list":
                return await lists.read_list(ctx.settings.lists_dir, args["list_name"])
            case "remove_from_list":
                return await lists.remove_from_list(
                    ctx.settings.lists_dir, args["list_name"], args["item"]
                )
            case "clear_list":
                return await lists.clear_list(ctx.settings.lists_dir, args["list_name"])
            case "which_lists":
                return await lists.which_lists(ctx.settings.lists_dir)
            case "add_current_to_playlist":
                return await music.add_current_to_playlist(ctx, args["playlist"])
            case "play_playlist":
                return await music.play_playlist(ctx, args["playlist"])
            case _:
                return f"Неизвестный инструмент: {name}"
    except Exception as exc:
        # Модель должна узнать об ошибке и попробовать иначе, а не молча зависнуть.
        log.exception("инструмент %s упал", name)
        return f"Ошибка при выполнении: {exc}"
