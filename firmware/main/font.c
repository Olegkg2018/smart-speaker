#include "font.h"

uint16_t happy_utf8_next(const char **p)
{
    const unsigned char *s = (const unsigned char *)*p;
    if (*s == 0) {
        return 0;
    }
    uint16_t code;
    if (*s < 0x80) {
        code = *s;
        s += 1;
    } else if ((*s & 0xE0) == 0xC0 && (s[1] & 0xC0) == 0x80) {
        code = (uint16_t)(((*s & 0x1F) << 6) | (s[1] & 0x3F));
        s += 2;
    } else if ((*s & 0xF0) == 0xE0 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80) {
        code = (uint16_t)(((*s & 0x0F) << 12) | ((s[1] & 0x3F) << 6) | (s[2] & 0x3F));
        s += 3;
    } else {
        // Битый или четырёхбайтный символ (эмодзи): в шрифте их нет, но и
        // застревать нельзя — пропускаем байт и идём дальше.
        code = '?';
        s += 1;
    }
    *p = (const char *)s;
    return code;
}

const happy_glyph_t *happy_font_glyph(uint16_t code)
{
    // Глифы отсортированы по code генератором — ищем двоично.
    int lo = 0, hi = happy_font.count - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        uint16_t c = happy_font.glyphs[mid].code;
        if (c == code) {
            return &happy_font.glyphs[mid];
        }
        if (c < code) {
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    return NULL;
}

int happy_font_text_width(const char *utf8)
{
    int w = 0;
    const char *p = utf8;
    uint16_t code;
    while ((code = happy_utf8_next(&p)) != 0) {
        const happy_glyph_t *g = happy_font_glyph(code);
        if (g == NULL) {
            g = happy_font_glyph('?');
        }
        if (g != NULL) {
            w += g->width;
        }
    }
    return w;
}
