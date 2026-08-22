"""Описания инструментов для модели и диспетчер вызовов.

Описания намеренно подробные и говорят, *когда* инструмент вызывать, а не
только что он делает: на этом заметно растёт доля правильных вызовов.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools import (
    alarms,
    homeassistant,
    lists,
    music,
    news,
    notes,
    timers,
    weather,
    websearch,
)
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
        "name": "set_alarm",
        "description": (
            "Будильник или напоминание на конкретное время: «разбуди завтра "
            "в шесть», «напомни в семь тридцать». Отличается от set_timer "
            "тем, что время абсолютное, а не «через сколько», и будильник "
            "переживает перезапуск — на утро ставить нужно именно его.\n"
            "Можно будить звуком: если человек сказал «разбуди шумом дождя» "
            "или «под музыку», передай это в sound."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": (
                        "Время в виде ЧЧ:ММ — «06:00». Без даты понимается "
                        "как ближайшее такое время: сказанное вечером «в "
                        "шесть» это завтрашнее утро. Если названа другая "
                        "дата, передавай целиком: «2026-08-25T06:00»."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Зачем будильник: «на работу». Необязательно.",
                },
                "sound": {
                    "type": "string",
                    "description": (
                        "Чем будить, если человек попросил: «шум дождя», "
                        "«спокойная музыка». Ищется как обычная музыка. "
                        "Не указывай, если про звук речи не было."
                    ),
                },
            },
            "required": ["when"],
        },
    },
    {
        "name": "list_alarms",
        "description": "Какие будильники стоят. Вызывай на «во сколько будильник».",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cancel_alarms",
        "description": "Отменить все будильники. Вызывай на «отмени будильник».",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "web_search",
        "description": (
            "Найти в интернете то, чего не знаешь и что не покрыто другими "
            "инструментами: курс валют, часы работы заведения, счёт матча, "
            "цены, справочные факты, недавние события. Вызывай, когда "
            "собственных сведений не хватает или они могли устареть — "
            "лучше поискать, чем ответить наугад.\n"
            "Не подменяй им другие инструменты: погоду спрашивай через "
            "get_weather, новости через get_news, музыку включай play_music."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Вопрос обычными словами, на языке собеседника. "
                        "Например: «курс доллара к гривне сегодня»."
                    ),
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Запомнить надолго: имена домашних, предпочтения, привычки, "
            "постоянные указания. Вызывай на «запомни, что…», «с этого "
            "момента…», «имей в виду».\n"
            "Это не то же самое, что список дел: сюда идёт знание о людях и "
            "доме, а не задачи. И не то же, что история разговора: она "
            "вытесняется новыми репликами, а это останется навсегда.\n"
            "Если распознанные слова не складываются в понятный факт — "
            "например, обрывок фразы или бессмыслица вперемешку с «запомни» "
            "— лучше переспросить, что именно запомнить, чем додумывать и "
            "сохранить неверное. Ошибочная запись остаётся навсегда, пока "
            "её не попросят забыть, а лишний уточняющий вопрос ничего не "
            "стоит."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": (
                        "Короткая фраза от третьего лица: «жену хозяина "
                        "зовут Марина», «в доме не едят мясо»."
                    ),
                }
            },
            "required": ["note"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Забыть ранее запомненное. Вызывай на «забудь про…». Удаляет "
            "все заметки, где встречается указанное слово."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "matching": {
                    "type": "string",
                    "description": "Слово из заметки, например «мясо».",
                }
            },
            "required": ["matching"],
        },
    },
    {
        "name": "list_notes",
        "description": "Что колонка помнит о доме. Вызывай на «что ты помнишь».",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_sensors",
        "description": (
            "Показания датчиков дома: температура, влажность, состояние "
            "дверей и розеток, заряд, любые сенсоры. Вызывай на «какая "
            "температура в спальне», «открыта ли дверь», «сколько градусов "
            "дома».\n"
            "Это быстрый прямой запрос к дому — используй его для вопросов "
            "«сколько» и «какое состояние», а не ask_home."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Часть названия датчика или комнаты: «спальня», "
                        "«температура», «дверь». Пусто — все датчики подряд."
                    ),
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask_home",
        "description": (
            "Передать команду умному дому его собственному ассистенту: свет, "
            "розетки, сцены, климат. Он знает все устройства по именам, "
            "которые им дал хозяин, поэтому фразу передавай почти как "
            "услышал: «включи свет в спальне», «выключи всё на кухне».\n"
            "Для вопросов о показаниях датчиков используй read_sensors — он "
            "быстрее и точнее."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phrase": {
                    "type": "string",
                    "description": "Команда дому обычными словами.",
                }
            },
            "required": ["phrase"],
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
            case "set_alarm":
                return await alarms.set_alarm(
                    ctx.settings.alarms_dir,
                    args["when"],
                    args.get("label"),
                    args.get("sound"),
                )
            case "list_alarms":
                return await alarms.list_alarms(ctx.settings.alarms_dir)
            case "cancel_alarms":
                return await alarms.cancel_alarms(ctx.settings.alarms_dir)
            case "web_search":
                return await websearch.web_search(
                    ctx.settings.openai_api_key,
                    ctx.settings.web_search_model,
                    args["query"],
                )
            case "remember":
                return await notes.remember(ctx.settings.notes_dir, args["note"])
            case "forget":
                return await notes.forget(ctx.settings.notes_dir, args["matching"])
            case "list_notes":
                return await notes.list_notes(ctx.settings.notes_dir)
            case "read_sensors":
                return await homeassistant.read_sensors(
                    ctx.settings.ha_url, ctx.settings.ha_token, args.get("query", "")
                )
            case "ask_home":
                if not ctx.settings.ha_allow_control:
                    # Управление пока выключено намеренно: ошибка
                    # распознавания не должна щёлкать выключателями.
                    return "Управление домом сейчас отключено, могу только читать датчики."
                return await homeassistant.ask_home(
                    ctx.settings.ha_url, ctx.settings.ha_token, args["phrase"]
                )
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
