#pragma once

#include <stddef.h>
#include <stdint.h>

// Растровый шрифт, запечённый в прошивку (tools/make_font.py).
//
// Экран рисует сама плата, поэтому шрифт обязан быть здесь: слать
// готовые картинки с сервера для цветного 240x320 нельзя — кадр RGB565
// весит около 150 КБ против одного килобайта у монохромного SSD1306,
// и такой поток пошёл бы по тому же каналу, что и звук каждые 20 мс.

typedef struct {
    uint16_t code;    // код символа в юникоде
    uint8_t width;    // ширина глифа в пикселях
    uint32_t offset;  // смещение битовой карты в data
} happy_glyph_t;

typedef struct {
    uint8_t height;                // высота строки в пикселях
    uint8_t ascent;                // от верха до базовой линии
    uint16_t count;                // сколько глифов
    const happy_glyph_t *glyphs;   // отсортированы по code — поиск двоичный
    const uint8_t *data;           // битовые карты подряд
} happy_font_t;

extern const happy_font_t happy_font;

// Глиф по коду символа или NULL, если такого в шрифте нет.
const happy_glyph_t *happy_font_glyph(uint16_t code);

// Ширина строки UTF-8 в пикселях — нужна для переноса и центрирования.
int happy_font_text_width(const char *utf8);

// Разбирает следующий символ UTF-8, двигает указатель. 0 — конец строки.
uint16_t happy_utf8_next(const char **p);
