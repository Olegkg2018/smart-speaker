#include "app.h"

#include <string.h>

#include "driver/i2c_master.h"
#include "esp_log.h"

static const char *TAG = "display";

#if CONFIG_HAPPY_SCREEN_ENABLED && !CONFIG_HAPPY_SCREEN_ST7789

#define SSD1306_ADDR 0x3C
#define SSD1306_CMD 0x00
#define SSD1306_DATA 0x40
#define FRAME_BYTES (HAPPY_SCREEN_WIDTH * HAPPY_SCREEN_HEIGHT / 8)

static i2c_master_dev_handle_t s_dev;
// Один байт префикса данных плюс кадр: контроллер ждёт их одной посылкой.
static uint8_t s_frame[1 + FRAME_BYTES];

static esp_err_t send_cmd(uint8_t cmd)
{
    const uint8_t payload[2] = {SSD1306_CMD, cmd};
    return i2c_master_transmit(s_dev, payload, sizeof(payload), 100);
}

static esp_err_t init_panel(void)
{
    // Стандартная последовательность включения SSD1306 128x64.
    static const uint8_t sequence[] = {
        0xAE,        // выключить панель на время настройки
        0x20, 0x00,  // горизонтальная адресация: кадр пишется одним потоком
        0xB0,        // начальная страница
        0xC8,        // сканирование строк сверху вниз
        0x00, 0x10,  // младший и старший полубайты колонки
        0x40,        // начальная строка
        0x81, 0x7F,  // контраст
        0xA1,        // отражение по горизонтали
        0xA6,        // без инверсии
        0xA8, 0x3F,  // мультиплексирование на 64 строки
        0xA4,        // содержимое из ОЗУ, не тест
        0xD3, 0x00,  // без смещения
        0xD5, 0x80,  // тактирование
        0xD9, 0xF1,  // фаза предзаряда
        0xDA, 0x12,  // конфигурация выводов COM
        0xDB, 0x40,  // уровень VCOMH
        0x8D, 0x14,  // включить внутренний преобразователь
        0xAF,        // включить панель
    };
    for (size_t i = 0; i < sizeof(sequence); i++) {
        esp_err_t err = send_cmd(sequence[i]);
        if (err != ESP_OK) {
            return err;
        }
    }
    return ESP_OK;
}

esp_err_t happy_display_start(void)
{
    i2c_master_bus_config_t bus_config = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = CONFIG_HAPPY_I2C_SDA,
        .scl_io_num = CONFIG_HAPPY_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_config, &bus));

    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = SSD1306_ADDR,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &dev_config, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "экран по адресу 0x%02X не отвечает — работаю без него", SSD1306_ADDR);
        s_dev = NULL;
        return ESP_OK;  // экран необязателен, колонка должна работать и без него
    }

    if (init_panel() != ESP_OK) {
        ESP_LOGW(TAG, "не удалось инициализировать экран");
        s_dev = NULL;
        return ESP_OK;
    }

    s_frame[0] = SSD1306_DATA;
    happy_display_clear();
    ESP_LOGI(TAG, "экран %dx%d на SDA%d/SCL%d", HAPPY_SCREEN_WIDTH, HAPPY_SCREEN_HEIGHT,
             CONFIG_HAPPY_I2C_SDA, CONFIG_HAPPY_I2C_SCL);
    return ESP_OK;
}

void happy_display_draw(const uint8_t *pages, size_t len)
{
    if (s_dev == NULL || len != FRAME_BYTES) {
        return;
    }
    // Адресуем весь экран целиком: сервер всегда присылает полный кадр,
    // частичных обновлений в протоколе нет.
    if (send_cmd(0x21) != ESP_OK) return;
    send_cmd(0x00);
    send_cmd(HAPPY_SCREEN_WIDTH - 1);
    send_cmd(0x22);
    send_cmd(0x00);
    send_cmd(HAPPY_SCREEN_HEIGHT / 8 - 1);

    memcpy(s_frame + 1, pages, len);
    i2c_master_transmit(s_dev, s_frame, sizeof(s_frame), 200);
}

void happy_display_clear(void)
{
    static const uint8_t blank[FRAME_BYTES] = {0};
    happy_display_draw(blank, sizeof(blank));
}

void happy_display_show(happy_state_t s, const char *t, const char *p)
{
    // На SSD1306 картинку целиком рисует сервер и присылает битмапом,
    // разбирать состояние и текст на плате незачем.
    (void)s; (void)t; (void)p;
}

#elif !CONFIG_HAPPY_SCREEN_ST7789  // экрана нет вовсе

esp_err_t happy_display_start(void) { return ESP_OK; }
void happy_display_draw(const uint8_t *pages, size_t len) { (void)pages; (void)len; }
void happy_display_clear(void) {}
void happy_display_show(happy_state_t s, const char *t, const char *p)
{
    (void)s; (void)t; (void)p;
}

#endif
