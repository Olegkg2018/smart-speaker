#!/usr/bin/env python3
"""Сателлит: выносной микрофон для колонки.

Слушает микрофон этого компьютера и шлёт звук в ту же сессию, где живёт
колонка. Сервер сам решает, кого слушать: у кого голос громче, тот и
источник реплики. Отвечает вслух всё равно колонка — у сателлита нет
динамика, только уши.

Нужен, чтобы проверить идею до того, как писать приложение для телефона:
действительно ли ближний микрофон улучшает распознавание.

    pip install sounddevice websockets numpy
    python3 tools/satellite.py --server ws://192.168.2.130:8090/stream

Полезное:
    --list                     показать микрофоны и выйти
    --device 3                 взять конкретный микрофон
    --name laptop              как называться в логах сервера
    --room home                к какой сессии присоединяться
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys

import numpy as np

try:
    import sounddevice as sd
    import websockets
except ImportError as exc:  # pragma: no cover — подсказка, а не логика
    sys.exit(f"не хватает зависимости: {exc}\n  pip install sounddevice websockets numpy")

RATE = 16_000
FRAME_MS = 20
FRAME = RATE // 1000 * FRAME_MS
FRAME_MIC = 0x01


def list_devices() -> None:
    print("Микрофоны:")
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            mark = "*" if i == sd.default.device[0] else " "
            print(f" {mark}{i:3d}  {dev['name']}  ({dev['max_input_channels']} кан.)")
    print("\n* — выбран по умолчанию")


async def run(args) -> None:
    frames: queue.Queue[bytes] = queue.Queue(maxsize=50)

    def on_audio(indata, _frames, _time, status):
        if status:
            print(f"звук: {status}", file=sys.stderr)
        try:
            frames.put_nowait(bytes(indata))
        except queue.Full:
            # Сеть не успевает — лучше потерять кадр, чем копить задержку:
            # сервер всё равно слушает не нас, если мы тише колонки.
            pass

    stream = sd.RawInputStream(
        samplerate=RATE, blocksize=FRAME, dtype="int16", channels=1, callback=on_audio
    )

    async with websockets.connect(args.server, max_size=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "t": "hello",
                    "device": args.name,
                    "room": args.room,
                    # Только слушаем: динамик и ответ — у колонки.
                    "role": "satellite",
                    "codec": "pcm",
                    "screen": False,
                    "fw": "satellite",
                }
            )
        )
        print(f"подключился как «{args.name}» в комнату «{args.room}»")

        async def show_server_messages():
            """Показывает, что происходит в сессии — в первую очередь кого слушают."""
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        continue  # звук и экран сателлиту не нужны
                    try:
                        data = json.loads(msg)
                    except ValueError:
                        continue
                    if data.get("t") == "state":
                        print(f"  состояние: {data.get('value')}")
                    elif data.get("t") == "ready":
                        print(f"  кодек: {data.get('codec')}")
            except Exception:
                pass

        reader = asyncio.create_task(show_server_messages())
        stream.start()
        loop = asyncio.get_running_loop()
        peak = 0.0
        shown = loop.time()

        try:
            while True:
                chunk = await loop.run_in_executor(None, frames.get)
                await ws.send(bytes([FRAME_MIC]) + chunk)

                # Показываем уровень: без него непонятно, слышит ли микрофон
                # вообще, и почему сервер выбирает не нас.
                level = float(np.abs(np.frombuffer(chunk, dtype=np.int16)).mean())
                peak = max(peak, level)
                now = loop.time()
                if now - shown >= 0.5:
                    bar = "#" * min(40, int(peak / 80))
                    print(f"\r  уровень {peak:6.0f} |{bar:<40}|", end="", flush=True)
                    peak = 0.0
                    shown = now
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            reader.cancel()
            stream.stop()
            stream.close()
            print("\nотключился")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="ws://192.168.2.130:8090/stream")
    ap.add_argument("--name", default="laptop", help="как называться серверу")
    ap.add_argument("--room", default="home", help="к какой сессии присоединиться")
    ap.add_argument("--device", type=int, default=None, help="номер микрофона")
    ap.add_argument("--list", action="store_true", help="показать микрофоны и выйти")
    args = ap.parse_args()

    if args.list:
        list_devices()
        return 0
    if args.device is not None:
        sd.default.device = (args.device, None)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
