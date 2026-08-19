"""Кодеки для аудиопотока колонки.

Opus экономит трафик в ~15 раз, но требует нативной библиотеки и на ESP32,
и на сервере. Поэтому поддерживаются оба варианта: колонка объявляет кодек
в `hello`, а на первом этапе разработки можно жить на сыром PCM.
"""

from __future__ import annotations

from typing import Protocol


class Codec(Protocol):
    """Кодирует и декодирует один кадр фиксированной длины."""

    name: str

    def encode(self, pcm: bytes) -> bytes: ...

    def decode(self, packet: bytes) -> bytes: ...


class PcmCodec:
    """Пропускает PCM s16le как есть. ~256 кбит/с на 16 кГц — Wi-Fi выдержит."""

    name = "pcm"

    def encode(self, pcm: bytes) -> bytes:
        return pcm

    def decode(self, packet: bytes) -> bytes:
        return packet


class OpusCodec:
    """Opus через `opuslib`. Ставится как `pip install happy-speaker[opus]`."""

    name = "opus"

    def __init__(self, sample_rate: int, frame_samples: int, bitrate: int = 24_000):
        import opuslib  # импорт внутри: зависимость необязательная

        self._frame_samples = frame_samples
        self._encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
        self._encoder.bitrate = bitrate
        self._decoder = opuslib.Decoder(sample_rate, 1)

    def encode(self, pcm: bytes) -> bytes:
        return self._encoder.encode(pcm, self._frame_samples)

    def decode(self, packet: bytes) -> bytes:
        return self._decoder.decode(packet, self._frame_samples)


def make_codec(name: str, sample_rate: int, frame_samples: int) -> Codec:
    """Создаёт кодек по имени; при отсутствии opuslib честно откатывается на PCM."""
    if name == "opus":
        try:
            return OpusCodec(sample_rate, frame_samples)
        except ImportError:
            # Лучше работать на PCM, чем не работать вовсе. Вызывающий код
            # смотрит на `codec.name`, чтобы сообщить клиенту фактический выбор.
            return PcmCodec()
    return PcmCodec()
