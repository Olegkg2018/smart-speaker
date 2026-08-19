"""Диалоговый агент на Claude Opus 5.

Цикл tool use написан вручную, а не через beta tool runner: нам нужно
перехватывать текст на уровне токенов и отдавать его в синтез по предложениям,
не дожидаясь конца ответа. Это главный источник ощущаемой скорости колонки.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from anthropic import AsyncAnthropic

from app.config import Settings
from app.memory import Turn
from app.tools.context import ToolContext
from app.tools.registry import TOOL_SCHEMAS, dispatch
from app.tts import SentenceBuffer

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — голосовой ассистент домашней умной колонки. Тебя слушают, а не читают.

Как говорить:
- Только по-русски, живой разговорной речью.
- Коротко: одна-три фразы. Длинный ответ в колонке невыносим.
- Никакой разметки: ни списков, ни звёздочек, ни заголовков, ни эмодзи. \
Всё это будет прочитано вслух как есть.
- Числа, даты и единицы пиши словами там, где иначе синтезатор прочитает \
их неправильно.
- Не проговаривай, что собираешься сделать, — просто делай и сообщай результат.

Как действовать:
- Если просьбу можно выполнить инструментом, вызывай инструмент сразу, \
не переспрашивая. «Включи что-нибудь бодрое» — это команда на музыку, \
а не повод уточнять жанр.
- Про новости, погоду и что происходит в мире ты сам по себе не знаешь \
ничего: твои сведения устарели. Любой вопрос о происходящем сейчас — \
это вызов инструмента, а не ответ по памяти. Не отговаривайся тем, \
что у тебя нет доступа к интернету: доступ есть, он и есть инструменты.
- Уточняй только если без ответа действие будет заведомо неверным.
- После инструмента скажи результат своими словами одной фразой, \
не пересказывай его дословно.
- Если не знаешь и инструмента нет — скажи это прямо и коротко.

Собеседник дома, часто занят руками и слушает вполуха. Отвечай так, \
чтобы всё было понятно с первого раза и без переспрашивания.
"""


class Agent:
    def __init__(self, settings: Settings, ctx: ToolContext):
        self._settings = settings
        self._ctx = ctx
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key or None)
        self._history: list[dict[str, Any]] = []

    def reset(self) -> None:
        self._history.clear()

    def seed_history(self, turns: list[Turn]) -> None:
        """Подсаживает сохранённый разговор при подключении колонки.

        Ответы ассистента из сохранённой истории — просто текст, без блоков
        tool_use: детали прошлых вызовов инструментов теряются, остаётся
        только смысл сказанного. Для голосового диалога этого достаточно.
        """
        self._history = [{"role": t.role, "content": t.text} for t in turns]
        self._trim_history()

    async def respond(
        self, user_text: str, on_sentence: Callable[[str], Awaitable[None]]
    ) -> None:
        """Обрабатывает реплику пользователя, отдавая ответ по предложениям."""
        self._history.append({"role": "user", "content": user_text})
        self._trim_history()
        sentences = SentenceBuffer()

        while True:
            message = await self._stream_turn(sentences, on_sentence)
            if message is None:
                return

            self._history.append({"role": "assistant", "content": message.content})

            if message.stop_reason != "tool_use":
                tail = sentences.flush()
                if tail:
                    await on_sentence(tail)
                return

            results = await self._run_tools(message.content)
            self._history.append({"role": "user", "content": results})

    async def _stream_turn(
        self, sentences: SentenceBuffer, on_sentence: Callable[[str], Awaitable[None]]
    ):
        """Один запрос к модели со стримингом. None — если модель отказала."""
        async with self._client.messages.stream(
            model=self._settings.model,
            max_tokens=self._settings.max_tokens,
            # Голосу нужна скорость. Мышление при этом остаётся включённым:
            # на Opus 5 с thinking=disabled модель иногда пишет вызов
            # инструмента обычным текстом, и он молча не выполняется.
            output_config={"effort": self._settings.effort},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Промпт не меняется между запросами — пусть кэшируется,
                    # если дорастёт до минимального размера кэша.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOL_SCHEMAS,
            messages=self._history,
        ) as stream:
            async for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type != "text_delta":
                    continue  # thinking_delta и прочее вслух не произносим
                for sentence in sentences.feed(event.delta.text):
                    await on_sentence(sentence)

            message = await stream.get_final_message()

        # Проверять stop_reason нужно до чтения content: при отказе
        # content пустой либо оборванный.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            log.warning("модель отказалась отвечать: %s", details)
            await on_sentence("Извини, на это я ответить не могу.")
            return None
        return message

    async def _run_tools(self, content: list[Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for block in content:
            if block.type != "tool_use":
                continue
            output = await dispatch(self._ctx, block.name, dict(block.input))
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output}
            )
        return results

    def _trim_history(self) -> None:
        """Обрезает историю, не разрывая пары tool_use / tool_result."""
        limit = self._settings.max_history_turns
        if len(self._history) <= limit:
            return
        tail = self._history[-limit:]
        # История обязана начинаться с настоящей реплики пользователя, иначе
        # API увидит tool_result без соответствующего tool_use.
        for i, message in enumerate(tail):
            if message["role"] == "user" and isinstance(message["content"], str):
                self._history = tail[i:]
                return
        self._history = tail[-1:] if tail[-1]["role"] == "user" else []
