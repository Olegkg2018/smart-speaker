"""Отрисовка экрана колонки на сервере.

Прошивке достаётся готовый битмап, а не текст: у ESP32 нет шрифтов
с кириллицей, а тащить туда шрифтовый движок ради строки статуса — лишнее.
Сервер и так рисует всё за доли миллисекунды.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.protocol import State

log = logging.getLogger(__name__)

_STATE_CAPTION: dict[State, str] = {
    State.IDLE: "готов",
    State.LISTENING: "слушаю",
    State.THINKING: "думаю",
    State.SPEAKING: "говорю",
    State.PLAYING: "играет",
}

_HEADER_HEIGHT = 14
_LINE_HEIGHT = 12


class ScreenRenderer:
    """Рисует кадр 128×64 и упаковывает его в формат страниц SSD1306."""

    def __init__(self, width: int, height: int, font_path: Path):
        self.width = width
        self.height = height
        self._font_path = font_path
        self._available = False
        self._font = None
        self._font_small = None
        self._image_mod = None
        self._draw_mod = None

    def load(self) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            log.warning("Pillow не установлен — экран колонки отключён")
            return
        if not self._font_path.exists():
            log.warning("шрифт %s не найден — экран колонки отключён", self._font_path)
            return

        self._image_mod = Image
        self._draw_mod = ImageDraw
        self._font = ImageFont.truetype(str(self._font_path), 11)
        self._font_small = ImageFont.truetype(str(self._font_path), 9)
        self._available = True
        log.info("экран %dx%d, шрифт %s", self.width, self.height, self._font_path.name)

    @property
    def available(self) -> bool:
        return self._available

    def render(self, state: State, text: str = "", now_playing: str | None = None) -> bytes:
        """Возвращает кадр в формате страниц SSD1306 (по 8 пикселей на байт)."""
        if not self._available:
            return b""

        image = self._image_mod.new("1", (self.width, self.height), 0)
        draw = self._draw_mod.Draw(image)

        # Шапка инверсией: состояние видно даже боковым зрением.
        draw.rectangle((0, 0, self.width - 1, _HEADER_HEIGHT - 1), fill=1)
        draw.text((3, 1), _STATE_CAPTION.get(state, "—"), font=self._font, fill=0)

        body_top = _HEADER_HEIGHT + 2
        body_lines = self.height - body_top
        max_lines = max(1, body_lines // _LINE_HEIGHT)

        if now_playing and not text:
            text = f"♪ {now_playing}"

        for i, line in enumerate(self._wrap(draw, text, max_lines)):
            draw.text((2, body_top + i * _LINE_HEIGHT), line, font=self._font, fill=1)

        return _to_pages(image, self.width, self.height)

    def _wrap(self, draw, text: str, max_lines: int) -> list[str]:
        """Переносит по словам под ширину экрана, лишнее прячет под многоточие."""
        if not text:
            return []
        lines: list[str] = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=self._font) <= self.width - 4:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)

        if len(lines) == max_lines and current not in lines[-1:]:
            lines[-1] = lines[-1][:-1] + "…"
        return lines[:max_lines]


def _to_pages(image, width: int, height: int) -> bytes:
    """PIL-картинка → раскладка SSD1306: байт это столбик из восьми пикселей."""
    pixels = image.load()
    buffer = bytearray(width * height // 8)
    for page in range(height // 8):
        for x in range(width):
            byte = 0
            for bit in range(8):
                if pixels[x, page * 8 + bit]:
                    byte |= 1 << bit
            buffer[page * width + x] = byte
    return bytes(buffer)
