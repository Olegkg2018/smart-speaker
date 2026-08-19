"""Протокол обмена между колонкой (ESP32) и сервером.

Один WebSocket. Бинарные фреймы — аудио, текстовые — управление (JSON).

Бинарный фрейм: 1 байт типа + полезная нагрузка.
    0x01  клиент → сервер, аудио с микрофона (16 кГц mono, кадр 20 мс)
    0x02  сервер → клиент, аудио на динамик (48 кГц mono, кадр 20 мс)
    0x03  сервер → клиент, готовая картинка для OLED (1 бит на пиксель)

Полезная нагрузка аудио — либо сырой PCM s16le, либо пакет Opus; что именно,
клиент сообщает в `hello` полем `codec`.

Картинку рисует сервер и присылает готовой: у прошивки нет ни шрифтов
с кириллицей, ни причин их заводить.
"""

from enum import StrEnum
from typing import Any

FRAME_MIC = 0x01
FRAME_SPEAKER = 0x02
FRAME_SCREEN = 0x03


class State(StrEnum):
    """Состояние сессии. Клиенту нужно только чтобы моргать светодиодом."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    PLAYING = "playing"


def pack_audio(kind: int, payload: bytes) -> bytes:
    return bytes((kind,)) + payload


def unpack_audio(frame: bytes) -> tuple[int, bytes]:
    if not frame:
        raise ValueError("пустой бинарный фрейм")
    return frame[0], frame[1:]


def state_msg(state: State) -> dict[str, Any]:
    return {"t": "state", "value": str(state)}


def volume_msg(level: float) -> dict[str, Any]:
    return {"t": "volume", "value": round(level, 3)}
