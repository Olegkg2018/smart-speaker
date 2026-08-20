#include "app.h"

#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "happy";

// Шаг громкости на одно нажатие. Десять шагов от тишины до максимума —
// достаточно точно и не требует долгого удержания.
#define VOLUME_STEP 0.1

static void on_button(happy_button_t button, bool pressed)
{
    if (!happy_ws_connected()) {
        ESP_LOGW(TAG, "нет связи с сервером — нажатие проигнорировано");
        return;
    }

    switch (button) {
    case HAPPY_BUTTON_TALK:
        // Тап, а не удержание: одно нажатие либо начинает слушать, либо
        // (если уже слушаем) досрочно останавливает запись — сервер сам
        // решает, что из двух. Отпускание ничего не шлёт: конец реплики
        // определяет сервер по тишине. Микрофон включаем сразу, не дожидаясь
        // ответа сервера — иначе первые слова после тапа потеряются.
        if (pressed) {
            happy_ws_send_json("{\"t\":\"ptt\",\"state\":\"down\"}");
            happy_audio_in_set_recording(true);
        }
        break;

    case HAPPY_BUTTON_VOL_DOWN:
        // Громкость знает сервер — просим сдвинуть, а не задаём значение.
        if (pressed) {
            happy_ws_send_json("{\"t\":\"volume_step\",\"value\":-0.1}");
        }
        break;

    case HAPPY_BUTTON_VOL_UP:
        if (pressed) {
            happy_ws_send_json("{\"t\":\"volume_step\",\"value\":0.1}");
        }
        break;
    }
}

// Сколько колонка терпит отсутствие связи, прежде чем перезагрузиться сама.
// Клиент WebSocket переподключается сам, но иногда стек залипает так, что
// переподключение не помогает: колонка молчит и на слово, и на кнопку, и
// оживает только выдёргиванием питания. Перезагрузка дешевле такого молчания.
#define WATCHDOG_TIMEOUT_MS (90 * 1000)
#define WATCHDOG_PERIOD_MS 5000

static void watchdog_task(void *arg)
{
    TickType_t offline_since = xTaskGetTickCount();

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(WATCHDOG_PERIOD_MS));

        if (happy_ws_connected()) {
            offline_since = xTaskGetTickCount();
            continue;
        }
        TickType_t offline_for = xTaskGetTickCount() - offline_since;
        if (offline_for > pdMS_TO_TICKS(WATCHDOG_TIMEOUT_MS)) {
            ESP_LOGE(TAG, "нет связи %d с — перезагружаюсь",
                     (int)(offline_for * portTICK_PERIOD_MS / 1000));
            esp_restart();
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "колонка «%s» запускается", CONFIG_HAPPY_DEVICE_NAME);

    // Индикацию и аудио поднимаем до сети: к моменту первого кадра тракт
    // уже готов, а светодиод сразу показывает, что связи пока нет.
    ESP_ERROR_CHECK(happy_led_start());
    ESP_ERROR_CHECK(happy_display_start());
    ESP_ERROR_CHECK(happy_audio_out_start());
    // Обработку поднимаем до микрофона: он сразу начнёт гнать через неё звук.
    ESP_ERROR_CHECK(happy_frontend_start());
    ESP_ERROR_CHECK(happy_audio_in_start());
    ESP_ERROR_CHECK(happy_button_start(on_button));

    ESP_ERROR_CHECK(happy_wifi_start());
    happy_wifi_wait_connected();
    ESP_ERROR_CHECK(happy_ws_start());

    if (xTaskCreate(watchdog_task, "watchdog", 2560, NULL, 2, NULL) != pdPASS) {
        ESP_LOGW(TAG, "сторож не запустился — зависание придётся лечить питанием");
    }

    ESP_LOGI(TAG, "готово: скажи активационную фразу или тапни кнопку");
}
