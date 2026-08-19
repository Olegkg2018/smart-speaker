#include "app.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "led";

#if CONFIG_HAPPY_LED_GPIO >= 0

#include "led_strip.h"

// Светодиод — единственная обратная связь, когда колонка молчит и экран занят
// текстом. Цвет несёт состояние, яркость держим низкой: она на виду.
#define BRIGHT 24
#define DIM 4

typedef struct {
    uint8_t r, g, b;
    int period_ms;  // 0 — ровно, >0 — мигание с этим полупериодом
} led_look_t;

static led_strip_handle_t s_strip;

static led_look_t look_for_state(void)
{
    if (!happy_ws_connected()) {
        return (led_look_t){BRIGHT, 0, 0, 150};  // красное частое — нет сервера
    }
    switch (happy_ws_state()) {
    case HAPPY_STATE_LISTENING:
        return (led_look_t){0, BRIGHT, 0, 0};  // зелёное ровно — слушаю
    case HAPPY_STATE_THINKING:
        return (led_look_t){0, 0, BRIGHT, 300};  // синее мигает — думаю
    case HAPPY_STATE_SPEAKING:
        return (led_look_t){0, BRIGHT / 2, BRIGHT, 600};  // голубое — говорю
    case HAPPY_STATE_PLAYING:
        return (led_look_t){DIM, 0, DIM, 0};  // тусклое сиреневое — играет музыка
    default:
        return (led_look_t){0, 0, 0, -1};  // погашено
    }
}

static void led_task(void *arg)
{
    bool on = false;

    while (true) {
        led_look_t look = look_for_state();

        if (look.period_ms == 0) {
            on = true;
        } else if (look.period_ms < 0) {
            on = false;
        } else {
            on = !on;
        }

        if (on) {
            led_strip_set_pixel(s_strip, 0, look.r, look.g, look.b);
        } else {
            led_strip_set_pixel(s_strip, 0, 0, 0, 0);
        }
        led_strip_refresh(s_strip);

        vTaskDelay(pdMS_TO_TICKS(look.period_ms > 0 ? look.period_ms : 200));
    }
}

esp_err_t happy_led_start(void)
{
    led_strip_config_t strip_config = {
        .strip_gpio_num = CONFIG_HAPPY_LED_GPIO,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
    };
    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_config, &rmt_config, &s_strip);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "светодиод не поднялся — работаю без него");
        return ESP_OK;  // индикация необязательна
    }
    led_strip_clear(s_strip);

    if (xTaskCreate(led_task, "led", 2560, NULL, 3, NULL) != pdPASS) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "светодиод WS2812 на GPIO%d", CONFIG_HAPPY_LED_GPIO);
    return ESP_OK;
}

#else

esp_err_t happy_led_start(void) { return ESP_OK; }

#endif
