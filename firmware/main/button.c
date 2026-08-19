#include "app.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "button";

// Механическая кнопка «звенит» несколько миллисекунд; 30 мс с запасом
// гасят дребезг, не мешая быстрым нажатиям.
#define DEBOUNCE_MS 30

typedef struct {
    int gpio;
    happy_button_t id;
    bool pressed;
} button_t;

static button_t s_buttons[] = {
    {CONFIG_HAPPY_BUTTON_GPIO, HAPPY_BUTTON_TALK, false},
#if CONFIG_HAPPY_VOL_DOWN_GPIO >= 0
    {CONFIG_HAPPY_VOL_DOWN_GPIO, HAPPY_BUTTON_VOL_DOWN, false},
#endif
#if CONFIG_HAPPY_VOL_UP_GPIO >= 0
    {CONFIG_HAPPY_VOL_UP_GPIO, HAPPY_BUTTON_VOL_UP, false},
#endif
};
#define BUTTON_COUNT (sizeof(s_buttons) / sizeof(s_buttons[0]))

static QueueHandle_t s_events;
static happy_button_cb_t s_callback;

static void IRAM_ATTR on_gpio_isr(void *arg)
{
    // В прерывании только будим задачу: уровни читаем уже после антидребезга.
    uint32_t token = (uint32_t)(uintptr_t)arg;
    xQueueSendFromISR(s_events, &token, NULL);
}

static void button_task(void *arg)
{
    uint32_t token;

    while (true) {
        if (xQueueReceive(s_events, &token, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(DEBOUNCE_MS));
        // Чистим события, накопившиеся за время дребезга.
        while (xQueueReceive(s_events, &token, 0) == pdTRUE) {
        }

        // Опрашиваем все кнопки разом: за время задержки могли нажать не одну.
        for (size_t i = 0; i < BUTTON_COUNT; i++) {
            // Кнопки замыкают на землю: нажата — низкий уровень.
            bool now = gpio_get_level(s_buttons[i].gpio) == 0;
            if (now == s_buttons[i].pressed) {
                continue;
            }
            s_buttons[i].pressed = now;
            ESP_LOGD(TAG, "GPIO%d %s", s_buttons[i].gpio, now ? "нажата" : "отпущена");
            if (s_callback != NULL) {
                s_callback(s_buttons[i].id, now);
            }
        }
    }
}

esp_err_t happy_button_start(happy_button_cb_t on_change)
{
    s_callback = on_change;
    s_events = xQueueCreate(16, sizeof(uint32_t));
    if (s_events == NULL) {
        return ESP_ERR_NO_MEM;
    }

    uint64_t mask = 0;
    for (size_t i = 0; i < BUTTON_COUNT; i++) {
        mask |= 1ULL << s_buttons[i].gpio;
    }
    gpio_config_t config = {
        .pin_bit_mask = mask,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_ANYEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&config));
    ESP_ERROR_CHECK(gpio_install_isr_service(0));
    for (size_t i = 0; i < BUTTON_COUNT; i++) {
        ESP_ERROR_CHECK(gpio_isr_handler_add(s_buttons[i].gpio, on_gpio_isr,
                                             (void *)(uintptr_t)i));
    }

    if (xTaskCreate(button_task, "button", 3072, NULL, 5, NULL) != pdPASS) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "кнопок подключено: %d, разговор на GPIO%d", (int)BUTTON_COUNT,
             CONFIG_HAPPY_BUTTON_GPIO);
    return ESP_OK;
}
