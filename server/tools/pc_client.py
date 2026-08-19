#!/usr/bin/env python3
"""Виртуальная колонка на ПК: микрофон и динамики ноутбука вместо ESP32.

Нужна, чтобы разрабатывать и отлаживать логику сервера, не трогая железо.
Говорит на том же протоколе, что и прошивка.

    pip install sounddevice websockets numpy
    python tools/pc_client.py ws://localhost:8080/stream

Enter — начать говорить, Enter ещё раз — закончить. Ctrl+C — выход.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import numpy as np
import sounddevice as sd
import websockets

MIC_RATE = 16_000
SPK_RATE = 48_000
FRAME_MS = 20
MIC_FRAME = MIC_RATE * FRAME_MS // 1000
SPK_FRAME = SPK_RATE * FRAME_MS // 1000

FRAME_MIC = 0x01
FRAME_SPEAKER = 0x02


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("uri", nargs="?", default="ws://localhost:8080/stream")
    parser.add_argument("--device", default="laptop")
    args = parser.parse_args()

    recording = asyncio.Event()
    loop = asyncio.get_running_loop()

    async with websockets.connect(args.uri, max_size=None) as ws:
        await ws.send(
            json.dumps({"t": "hello", "device": args.device, "fw": "pc", "codec": "pcm"})
        )
        print(f"подключено к {args.uri}")

        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        speaker_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)

        def on_mic(indata, frames, time_info, status) -> None:
            # Колбэк живёт в потоке PortAudio — в цикл событий кладём потокобезопасно.
            if recording.is_set():
                loop.call_soon_threadsafe(_put_nowait, mic_queue, bytes(indata))

        def on_speaker(outdata, frames, time_info, status) -> None:
            try:
                chunk = speaker_queue.get_nowait()
            except asyncio.QueueEmpty:
                outdata[:] = b"\x00" * len(outdata)
                return
            outdata[: len(chunk)] = chunk
            if len(chunk) < len(outdata):
                outdata[len(chunk) :] = b"\x00" * (len(outdata) - len(chunk))

        mic_stream = sd.RawInputStream(
            samplerate=MIC_RATE, blocksize=MIC_FRAME, dtype="int16",
            channels=1, callback=on_mic,
        )
        speaker_stream = sd.RawOutputStream(
            samplerate=SPK_RATE, blocksize=SPK_FRAME, dtype="int16",
            channels=1, callback=on_speaker,
        )

        with mic_stream, speaker_stream:
            await asyncio.gather(
                _send_mic(ws, mic_queue),
                _receive(ws, speaker_queue),
                _push_to_talk(ws, recording),
            )


def _put_nowait(queue: asyncio.Queue, item: bytes) -> None:
    if not queue.full():
        queue.put_nowait(item)


async def _send_mic(ws, queue: asyncio.Queue[bytes]) -> None:
    while True:
        pcm = await queue.get()
        await ws.send(bytes((FRAME_MIC,)) + pcm)


async def _receive(ws, speaker_queue: asyncio.Queue[bytes]) -> None:
    async for message in ws:
        if isinstance(message, bytes):
            if message and message[0] == FRAME_SPEAKER:
                _put_nowait(speaker_queue, message[1:])
            continue
        data = json.loads(message)
        if data.get("t") == "state":
            print(f"  [{data['value']}]")
        elif data.get("t") == "ready":
            print(f"  сервер готов, кодек {data['codec']}")


async def _push_to_talk(ws, recording: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, sys.stdin.readline)
        if recording.is_set():
            recording.clear()
            await ws.send(json.dumps({"t": "ptt", "state": "up"}))
            print("… обрабатываю")
        else:
            recording.set()
            await ws.send(json.dumps({"t": "ptt", "state": "down"}))
            print("говорите (Enter — закончить)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nпока")
