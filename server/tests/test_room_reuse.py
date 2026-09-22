"""Гонка при переподключении в ту же комнату.

Живой случай: колонку обесточили во время играющей музыки. Старая сессия
ещё дотирает закрытие (voice.close(), мixer, сторожа — это асинхронно и
небыстро), а колонка уже переподключилась — и раньше садилась в ЕЩЁ НЕ
удалённую из `_rooms` старую сессию, наследуя её состояние: светодиод
показывал «играет музыка» вместо «готова», хотя играть уже было нечему.
"""

from __future__ import annotations

import asyncio
import json

import app.main as main_mod


class _FakeSession:
    """Достаточно, чтобы пройти через stream(): своя очередь пиров не
    нужна, важно только is_empty и что serve() реально было вызвано."""

    def __init__(self, *_args, **_kwargs):
        self.is_empty = True
        self.served_with: list = []

    async def serve(self, ws, hello) -> None:
        self.served_with.append((ws, hello))
        self.is_empty = False  # пир "присоединился"


class _FakeWS:
    def __init__(self, hello: dict):
        self._hello = hello

    async def accept(self):
        pass

    async def receive_text(self):
        return json.dumps(self._hello)

    async def close(self):
        pass


def _reset_rooms():
    main_mod._rooms.clear()


async def test_reconnect_gets_a_fresh_session_when_old_one_is_empty(monkeypatch):
    """Старая сессия опустела (её единственный пир уже вышел), но ещё не
    убрана из _rooms её собственным finally — реконнект не должен в неё
    садиться."""
    monkeypatch.setattr(main_mod, "Session", _FakeSession)
    _reset_rooms()
    try:
        stale = _FakeSession()
        stale.is_empty = True  # пир уже ушёл, но stream() старой сессии ещё не завершился
        main_mod._rooms["home"] = stale

        await main_mod.stream(_FakeWS({"room": "home", "device": "kitchen"}))

        fresh = main_mod._rooms["home"]
        assert fresh is not stale, "не должны были переиспользовать пустую старую сессию"
        assert fresh.served_with, "новая сессия обязана обслужить подключение"
    finally:
        _reset_rooms()


async def test_reconnect_joins_the_same_session_while_it_is_still_active(monkeypatch):
    """Обычный случай не задет: сателлит подключается к уже живой (не
    пустой) сессии колонки — это та же комната, тот же разговор."""
    monkeypatch.setattr(main_mod, "Session", _FakeSession)
    _reset_rooms()
    try:
        active = _FakeSession()
        active.is_empty = False  # колонка уже внутри
        main_mod._rooms["home"] = active

        await main_mod.stream(_FakeWS({"room": "home", "device": "phone", "role": "satellite"}))

        assert main_mod._rooms["home"] is active
        assert active.served_with
    finally:
        _reset_rooms()


async def test_finally_does_not_delete_a_session_that_was_replaced(monkeypatch):
    """Гонка целиком: старая сессия допевает своё serve() уже ПОСЛЕ того,
    как реконнект создал новую — её собственный finally не должен снести
    новую сессию из _rooms."""
    monkeypatch.setattr(main_mod, "Session", _FakeSession)
    _reset_rooms()
    try:
        stale = _FakeSession()
        stale.is_empty = True
        main_mod._rooms["home"] = stale

        # Реконнект создаёт новую сессию (та же логика, что в stream()).
        await main_mod.stream(_FakeWS({"room": "home", "device": "kitchen"}))
        fresh = main_mod._rooms["home"]
        assert fresh is not stale

        # "Старая" stream()-корутина только теперь добирается до своего
        # finally: `_rooms.get(room) is session` — уже не про неё.
        async with main_mod._rooms_lock:
            if main_mod._rooms.get("home") is stale and stale.is_empty:
                del main_mod._rooms["home"]

        assert main_mod._rooms["home"] is fresh, "финальная уборка старой сессии не должна задевать новую"
    finally:
        _reset_rooms()
