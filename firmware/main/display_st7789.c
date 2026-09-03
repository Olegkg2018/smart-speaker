#include "app.h"
#include "font.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_check.h"
#include "esp_log.h"

#if CONFIG_HAPPY_SCREEN_ST7789

static const char *TAG = "st7789";

// Панель поднимается штатным esp_lcd из ESP-IDF — тем же способом, что и в
// 78/xiaozhi-esp32 (boards/bread-compact-wifi-lcd). Их вывод на экран
// построен на LVGL и C++, это к нам не переносится, а вот инициализация
// панели — обычный esp_lcd и переносится один в один. Распиновка взята
// оттуда же намеренно: одна и та же пайка работает с обеими прошивками.

#define LCD_HOST SPI3_HOST
#define LCD_W CONFIG_HAPPY_SCREEN_WIDTH
#define LCD_H CONFIG_HAPPY_SCREEN_HEIGHT

// Цельный кадр RGB565 240x320 — это 150 КБ, во внутренней памяти столько
// не занять: её на плате около четверти мегабайта, и она уже поделена со
// звуковым буфером. Рисуем полосами: одна строка текста за раз.
// 32 пикселя с запасом перекрывают шрифт высотой 24.
#define STRIPE_H 32

static esp_lcd_panel_handle_t s_panel;
static esp_lcd_panel_io_handle_t s_io;
static uint16_t *s_stripe;  // буфер полосы, LCD_W * STRIPE_H пикселей

static inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    // Панель ждёт big-endian, а esp_lcd отдаёт как есть — разворачиваем тут.
    uint16_t c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
    return (uint16_t)((c >> 8) | (c << 8));
}

esp_err_t happy_display_start(void)
{
    spi_bus_config_t bus = {
        .mosi_io_num = CONFIG_HAPPY_LCD_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = CONFIG_HAPPY_LCD_CLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LCD_W * STRIPE_H * sizeof(uint16_t),
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO), TAG,
                        "шина SPI не поднялась");

    esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num = CONFIG_HAPPY_LCD_CS,
        .dc_gpio_num = CONFIG_HAPPY_LCD_DC,
        .spi_mode = 0,
        .pclk_hz = 40 * 1000 * 1000,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_RETURN_ON_ERROR(
        esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_cfg, &s_io),
        TAG, "panel io не создался");

    esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = CONFIG_HAPPY_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7789(s_io, &panel_cfg, &s_panel), TAG,
                        "панель ST7789 не создалась");

    ESP_ERROR_CHECK(esp_lcd_panel_reset(s_panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel));
    // Этой панели нужна инверсия — как и в конфиге xiaozhi для того же экрана.
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(s_panel, true));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(s_panel, true));

    // Полоса живёт в PSRAM: внутреннюю память бережём под звук.
    s_stripe = heap_caps_malloc(LCD_W * STRIPE_H * sizeof(uint16_t),
                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_stripe == NULL) {
        ESP_LOGE(TAG, "не хватило памяти на буфер полосы");
        return ESP_ERR_NO_MEM;
    }

    // Проверка обязана быть на этапе компиляции: при -1 (подсветка заведена
    // внутри модуля) выражение 1ULL << -1 не собирается без предупреждения,
    // даже если ветка недостижима.
#if CONFIG_HAPPY_LCD_BACKLIGHT >= 0
    gpio_config_t bl = {
        .pin_bit_mask = 1ULL << CONFIG_HAPPY_LCD_BACKLIGHT,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&bl);
    gpio_set_level(CONFIG_HAPPY_LCD_BACKLIGHT, 1);
#endif

    happy_display_clear();
    ESP_LOGI(TAG, "экран ST7789 %dx%d готов", LCD_W, LCD_H);
    return ESP_OK;
}

void happy_display_fill(int y, int h, uint16_t color)
{
    if (s_panel == NULL || s_stripe == NULL || h <= 0) {
        return;
    }
    if (h > STRIPE_H) {
        h = STRIPE_H;
    }
    for (int i = 0; i < LCD_W * h; i++) {
        s_stripe[i] = color;
    }
    esp_lcd_panel_draw_bitmap(s_panel, 0, y, LCD_W, y + h, s_stripe);
}

void happy_display_clear(void)
{
    for (int y = 0; y < LCD_H; y += STRIPE_H) {
        int h = (y + STRIPE_H > LCD_H) ? (LCD_H - y) : STRIPE_H;
        happy_display_fill(y, h, rgb565(0, 0, 0));
    }
}

// Рисует строку в буфер полосы. Возвращает, сколько пикселей заняло.
static int draw_text_to_stripe(const char *utf8, int x, uint16_t fg, uint16_t bg)
{
    const happy_font_t *f = &happy_font;
    for (int i = 0; i < LCD_W * f->height; i++) {
        s_stripe[i] = bg;
    }

    const char *p = utf8;
    uint16_t code;
    while ((code = happy_utf8_next(&p)) != 0 && x < LCD_W) {
        const happy_glyph_t *g = happy_font_glyph(code);
        if (g == NULL) {
            g = happy_font_glyph('?');
            if (g == NULL) {
                continue;
            }
        }
        int row_bytes = (g->width + 7) / 8;
        const uint8_t *bits = f->data + g->offset;
        for (int gy = 0; gy < f->height; gy++) {
            for (int gx = 0; gx < g->width; gx++) {
                int px = x + gx;
                if (px < 0 || px >= LCD_W) {
                    continue;
                }
                uint8_t byte = bits[gy * row_bytes + gx / 8];
                if (byte & (0x80 >> (gx % 8))) {
                    s_stripe[gy * LCD_W + px] = fg;
                }
            }
        }
        x += g->width;
    }
    return x;
}

void happy_display_text(int y, const char *utf8, uint32_t rgb)
{
    if (s_panel == NULL || s_stripe == NULL || utf8 == NULL) {
        return;
    }
    if (y + happy_font.height > LCD_H) {
        return;
    }
    uint16_t fg = rgb565((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
    draw_text_to_stripe(utf8, 0, fg, rgb565(0, 0, 0));
    esp_lcd_panel_draw_bitmap(s_panel, 0, y, LCD_W, y + happy_font.height, s_stripe);
}

// Старый путь «сервер прислал готовый битмап» на цветном экране не
// используется: смысл переезда в том, что кадр больше не летит по сети.
void happy_display_draw(const uint8_t *pages, size_t len)
{
    (void)pages;
    (void)len;
}

// --- компоновка экрана ---

#define PAD 6
#define LINE (happy_font.height + 2)

static const char *state_caption(happy_state_t st)
{
    switch (st) {
    case HAPPY_STATE_LISTENING: return "Слушаю";
    case HAPPY_STATE_THINKING:  return "Думаю";
    case HAPPY_STATE_SPEAKING:  return "Отвечаю";
    default:                    return "Готова";
    }
}

static uint32_t state_color(happy_state_t st)
{
    switch (st) {
    case HAPPY_STATE_LISTENING: return 0x33CC66;  // зелёный
    case HAPPY_STATE_THINKING:  return 0xFFAA22;  // янтарный
    case HAPPY_STATE_SPEAKING:  return 0x3399FF;  // синий
    default:                    return 0x777777;  // серый
    }
}

// Переносит текст по словам и рисует, начиная с y. Возвращает следующий y.
static int draw_wrapped(int y, const char *utf8, uint32_t rgb, int max_lines)
{
    char line[128];
    size_t line_len = 0;
    int lines = 0;
    const char *word = utf8;
    const char *p = utf8;

    while (lines < max_lines) {
        // Ищем конец слова.
        const char *scan = p;
        uint16_t code;
        while ((code = happy_utf8_next(&scan)) != 0 && code != ' ') {
            p = scan;
        }
        size_t wlen = (size_t)(p - word);
        if (wlen == 0 && code == 0) {
            break;
        }

        char candidate[128];
        size_t clen = line_len;
        if (clen + wlen + 1 >= sizeof(candidate)) {
            wlen = sizeof(candidate) - clen - 2;
        }
        memcpy(candidate, line, line_len);
        if (line_len > 0) {
            candidate[clen++] = ' ';
        }
        memcpy(candidate + clen, word, wlen);
        candidate[clen + wlen] = '\0';

        if (happy_font_text_width(candidate) > LCD_W - 2 * PAD && line_len > 0) {
            line[line_len] = '\0';
            happy_display_text(y, line, rgb);
            y += LINE;
            lines++;
            line_len = 0;
            continue;  // это же слово попробуем на новой строке
        }

        memcpy(line, candidate, clen + wlen + 1);
        line_len = clen + wlen;

        if (code == 0) {
            break;
        }
        p = scan;
        word = p;
    }

    if (line_len > 0 && lines < max_lines) {
        line[line_len] = '\0';
        happy_display_text(y, line, rgb);
        y += LINE;
    }
    return y;
}

void happy_display_show(happy_state_t state, const char *text, const char *playing)
{
    static char s_text[192];
    static char s_playing[96];

    if (text != NULL) {
        strlcpy(s_text, text, sizeof(s_text));
    }
    if (playing != NULL) {
        strlcpy(s_playing, playing, sizeof(s_playing));
    }
    if (s_panel == NULL) {
        return;
    }

    happy_display_clear();

    int y = PAD;
    happy_display_text(y, state_caption(state), state_color(state));
    y += LINE + 4;

    if (s_text[0] != '\0') {
        y = draw_wrapped(y, s_text, 0xFFFFFF, 8);
    }

    if (s_playing[0] != '\0') {
        int py = LCD_H - LINE - PAD;
        if (py > y) {
            happy_display_text(py, s_playing, 0x888888);
        }
    }
}

#endif  // CONFIG_HAPPY_SCREEN_ST7789
