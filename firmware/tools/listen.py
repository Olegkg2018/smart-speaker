#!/usr/bin/env python3
"""Слушает звук, который колонка дублирует по UDP.

Нужен, чтобы не гадать о качестве распознавания по косвенным признакам.
За эту неделю мы дважды разбирались, слышит ли микрофон человека или
эхо собственного динамика, — по расшифровкам на румынском и по доле
опоздавших кадров. Проще послушать.

Включается в прошивке: menuconfig → Happy Speaker → Диагностика →
«Дублировать звук микрофона по UDP», там же адрес этого компьютера.

    python3 tools/listen.py                 # писать в mic.wav
    python3 tools/listen.py --play          # ещё и играть сразу (нужен aplay)
    python3 tools/listen.py -o /tmp/x.wav --seconds 30

Поток сырой: PCM 16 бит, моно, 16 кГц, без заголовков.
"""

import argparse
import socket
import subprocess
import sys
import time
import wave

SAMPLE_RATE = 16_000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-p", "--port", type=int, default=9999)
    ap.add_argument("-o", "--out", default="mic.wav", help="куда записать")
    ap.add_argument("--seconds", type=float, default=0, help="0 — до Ctrl-C")
    ap.add_argument("--play", action="store_true", help="играть на ходу через aplay")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)

    player = None
    if args.play:
        try:
            player = subprocess.Popen(
                ["aplay", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1"],
                stdin=subprocess.PIPE,
            )
        except FileNotFoundError:
            print("aplay не найден — только записываю в файл", file=sys.stderr)

    print(f"слушаю порт {args.port}, пишу в {args.out}. Ctrl-C — закончить.")
    started = time.time()
    total = 0
    silent_since = time.time()

    wf = wave.open(args.out, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                # Тишина в эфире — это тоже диагностика: значит колонка
                # не шлёт (не собрана с отладкой, не тот адрес, нет сети).
                if time.time() - silent_since > 5:
                    print("…пять секунд ничего не приходит", file=sys.stderr)
                    silent_since = time.time()
                continue

            silent_since = time.time()
            wf.writeframes(data)
            total += len(data)
            if player is not None and player.stdin is not None:
                try:
                    player.stdin.write(data)
                except BrokenPipeError:
                    player = None

            secs = total / 2 / SAMPLE_RATE
            print(f"\rзаписано {secs:6.1f} с", end="", file=sys.stderr)
            if args.seconds and time.time() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        wf.close()
        if player is not None and player.stdin is not None:
            player.stdin.close()
        print(f"\nготово: {args.out}, {total / 2 / SAMPLE_RATE:.1f} с звука")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
