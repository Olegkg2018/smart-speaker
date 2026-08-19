#include "app.h"

#include "esp_log.h"
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

void app_main(void)
{
    ESP_LOGI(TAG, "колонка «%s» запускается", CONFIG_HAPPY_DEVICE_NAME);

    // Индикацию и аудио поднимаем до сети: к моменту первого кадра тракт
    // уже готов, а светодиод сразу показывает, что связи пока нет.
    ESP_ERROR_CHECK(happy_led_start());
    ESP_ERROR_CHECK(happy_display_start());
    ESP_ERROR_CHECK(happy_audio_out_start());
    ESP_ERROR_CHECK(happy_audio_in_start());
    ESP_ERROR_CHECK(happy_button_start(on_button));

    ESP_ERROR_CHECK(happy_wifi_start());
    happy_wifi_wait_connected();
    ESP_ERROR_CHECK(happy_ws_start());

    ESP_LOGI(TAG, "готово, тапни кнопку и говори");
}
