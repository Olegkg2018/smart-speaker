"""Дублирование разговора в Telegram.

Колонка живёт без экрана и истории под рукой: что было сказано час назад,
восстановить неоткуда. Отправка в личные сообщения делает разговор
просматриваемым с телефона — и заодно показывает, что колонка расслышала,
когда ответ выглядит странно.

Реплики уходят парами «вопрос — ответ», а не по одной: два сообщения на
каждую фразу превращают переписку в свалку уведомлений.
"""

from __future__ import annotations

import asyncio
import html
import logging

import httpx

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram режет сообщения длиннее этого, и лучше обрезать самим — с
# понятным хвостом, а не на полуслове.
_MAX_CHARS = 3500

# Сколько ждать ответ ассистента, прежде чем отправить вопрос в одиночку.
# Ответ обычно приходит за секунды, но реплика могла и оборваться.
_PAIR_TIMEOUT_S = 30.0


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, device: str = ""):
        self._token = token.strip()
        self._chat_id = chat_id.strip()
        self._device = device
        self._pending_question: str | None = None
        self._flush_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def on_turn(self, role: str, text: str) -> None:
        """Реплика прозвучала. Вопрос придерживаем до ответа на него."""
        if not self.enabled or not text.strip():
            return

        if role == "user":
            # Прошлый вопрос остался без ответа — отправляем как есть,
            # иначе он потеряется под новым.
            await self._flush_pending()
            self._pending_question = text.strip()
            self._schedule_flush()
            return

        question, self._pending_question = self._pending_question, None
        self._cancel_flush()
        if question:
            await self._send(f"🗣 {question}\n\n💬 {text.strip()}")
        else:
            await self._send(f"💬 {text.strip()}")

    async def close(self) -> None:
        self._cancel_flush()
        await self._flush_pending()

    def _schedule_flush(self) -> None:
        self._cancel_flush()
        self._flush_task = asyncio.create_task(self._flush_later())

    def _cancel_flush(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(_PAIR_TIMEOUT_S)
            await self._flush_pending()
        except asyncio.CancelledError:
            raise

    async def _flush_pending(self) -> None:
        question, self._pending_question = self._pending_question, None
        if question:
            await self._send(f"🗣 {question}\n\n(без ответа)")

    async def _send(self, text: str) -> None:
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "…"
        head = f"<b>{html.escape(self._device)}</b>\n" if self._device else ""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    _API.format(token=self._token),
                    json={
                        "chat_id": self._chat_id,
                        "text": head + html.escape(text),
                        "parse_mode": "HTML",
                        # Разговор идёт непрерывно: звук на каждую реплику
                        # превратил бы телефон в трещотку.
                        "disable_notification": True,
                    },
                )
            if response.status_code != 200:
                log.warning("Telegram не принял сообщение: %s", response.text[:200])
        except httpx.HTTPError as exc:
            # Пересылка — удобство, а не работа колонки: молча переживаем.
            log.warning("не удалось отправить в Telegram: %s", exc)
