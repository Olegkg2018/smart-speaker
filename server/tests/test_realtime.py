"""Приём событий от облака не должен глушить отправку звука на колонку."""

import asyncio

from app.realtime import RealtimeVoice


class _BurstConnection:
    """Соединение, у которого пачка событий уже лежит в буфере.

    Именно так ведёт себя настоящий сокет: облако присылает несколько
    десятков кусков звука разом, и каждый следующий доступен немедленно,
    без ожидания сети.
    """

    def __init__(self, count: int) -> None:
        self._left = count

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._left == 0:
            raise StopAsyncIteration
        self._left -= 1
        return object()


async def test_recv_loop_lets_the_sender_run():
    """Отправщик кадров обязан получать управление посреди пачки событий.

    Кадр звука уходит на колонку каждые 20 мс. Пока цикл приёма
    прокручивал всю пачку не уступая, отправщик просто стоял, и речь
    рвалась с опозданием до трёхсот миллисекунд.
    """
    voice = RealtimeVoice.__new__(RealtimeVoice)
    voice._conn = _BurstConnection(50)

    async def _noop(_event):
        return None

    voice._on_event = _noop

    ticks = 0

    async def sender():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    pump = asyncio.create_task(sender())
    await voice._recv_loop()
    pump.cancel()

    # Без уступки соседняя задача успела бы провернуться считанные разы.
    assert ticks >= 50, f"отправщик получил управление всего {ticks} раз"
