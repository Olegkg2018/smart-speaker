"""Линейная передискретизация PCM16 mono. Общий код для TTS и Realtime."""

from __future__ import annotations

import numpy as np


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Меняет частоту дискретизации. Для речи линейной интерполяции достаточно."""
    if not pcm or src_rate == dst_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    out_len = int(len(samples) * dst_rate / src_rate)
    if out_len <= 0:
        return b""
    src_idx = np.linspace(0, len(samples) - 1, out_len, dtype=np.float32)
    resampled = np.interp(src_idx, np.arange(len(samples), dtype=np.float32), samples)
    return resampled.astype(np.int16).tobytes()
