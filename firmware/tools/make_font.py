#!/usr/bin/env python3
"""Собирает растровый шрифт с кириллицей в C-массив для прошивки.

Экран рисует сама плата, значит и шрифт должен жить в прошивке. Берём тот
же DejaVuSans, которым сервер рисовал монохромный SSD1306, и запекаем из
него только нужные символы: латиница, кириллица, цифры, знаки. Полный
юникод сюда не влезет и не нужен.

Каждый глиф — битовая карта фиксированной высоты, по строке на байтовый
ряд. Ширина у символов разная (пропорциональный шрифт читается лучше
моноширинного на узком экране), поэтому храним её рядом с глифом.

Запуск:  python3 tools/make_font.py 20 > main/font20.c
"""

import sys

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Что запекаем: печатная латиница, кириллица, «ё», типографские кавычки и
# тире, которые часто приходят из ответов модели.
CHARS = (
    [chr(c) for c in range(0x20, 0x7F)]
    + [chr(c) for c in range(0x410, 0x450)]
    + ["Ё", "ё", "«", "»", "—", "–", "…", "°", "№"]
)


def main() -> int:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    font = ImageFont.truetype(FONT_PATH, size)

    ascent, descent = font.getmetrics()
    height = ascent + descent

    glyphs = []
    # По коду: прошивка ищет глиф двоичным поиском, и порядок здесь —
    # часть контракта с happy_font_glyph().
    for ch in sorted(set(CHARS), key=ord):
        w = int(round(font.getlength(ch)))
        w = max(w, 1)
        img = Image.new("1", (w, height), 0)
        ImageDraw.Draw(img).text((0, 0), ch, font=font, fill=1)

        row_bytes = (w + 7) // 8
        data = bytearray()
        px = img.load()
        for y in range(height):
            for bx in range(row_bytes):
                byte = 0
                for bit in range(8):
                    x = bx * 8 + bit
                    if x < w and px[x, y]:
                        byte |= 0x80 >> bit
                data.append(byte)
        glyphs.append((ch, w, bytes(data)))

    out = sys.stdout
    out.write("// Сгенерировано tools/make_font.py — правки затрутся.\n")
    out.write('#include "font.h"\n\n')

    out.write("static const uint8_t glyph_data[] = {\n")
    offsets = []
    pos = 0
    for _ch, _w, data in glyphs:
        offsets.append(pos)
        for i in range(0, len(data), 16):
            out.write("    " + "".join(f"0x{b:02x}," for b in data[i : i + 16]) + "\n")
        pos += len(data)
    out.write("};\n\n")

    out.write("static const happy_glyph_t glyphs[] = {\n")
    for (ch, w, _data), off in zip(glyphs, offsets):
        out.write(f"    {{0x{ord(ch):04x}, {w}, {off}}},  // {ch!r}\n")
    out.write("};\n\n")

    out.write("const happy_font_t happy_font = {\n")
    out.write(f"    .height = {height},\n")
    out.write(f"    .ascent = {ascent},\n")
    out.write(f"    .count = {len(glyphs)},\n")
    out.write("    .glyphs = glyphs,\n")
    out.write("    .data = glyph_data,\n")
    out.write("};\n")

    total = sum(len(d) for _c, _w, d in glyphs)
    print(
        f"// глифов {len(glyphs)}, высота {height}, данные {total} байт",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
