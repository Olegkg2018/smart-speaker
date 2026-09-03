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
            // По кнопке начало реплики известно точно — накопленное до
            // нажатия только добавит случайного фона.
            happy_wake_cache_clear();
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

static void send_mic_frame(const int16_t *pcm, size_t samples)
{
    // Дубль на компьютер для прослушивания — до всех условий: интересно
    // именно то, что слышит микрофон, а не то, что дошло до сервера.
    happy_audio_debug_feed(pcm, samples);

    // Микрофон слушает непрерывно ради активационного слова, но в сеть
    // звук уходит только во время реплики: круглосуточный поток забивал
    // Wi-Fi и заставлял сервер считать то, что ему не нужно.
    if (happy_audio_in_is_recording()) {
        happy_ws_send_mic((const uint8_t *)pcm, samples * sizeof(int16_t));
    } else {
        // Пока не пишем — копим в кольцо. В момент активации накопленное
        // уйдёт первым, иначе начало команды теряется.
        happy_wake_cache_store(pcm, samples);
    }
}

static void send_cached_frame(const int16_t *pcm, size_t samples)
{
    happy_ws_send_mic((const uint8_t *)pcm, samples * sizeof(int16_t));
}

static void on_wake_word(void)
{
    if (!happy_ws_connected()) {
        return;
    }
    // Для сервера это то же самое, что тап по кнопке: он сам решит,
    // начать слушать или прервать текущий ответ.
    happy_ws_send_json("{\"t\":\"ptt\",\"state\":\"down\"}");
    happy_audio_in_set_recording(true);
    // Сначала — то, что человек успел сказать до срабатывания WakeNet,
    // и только потом живой поток. Порядок важен: иначе начало команды
    // приедет после её продолжения.
    size_t primed = happy_wake_cache_drain(send_cached_frame);
    if (primed > 0) {
        ESP_LOGI(TAG, "досланы %u мс звука до активационного слова",
                 (unsigned)(primed * 1000 / HAPPY_MIC_SAMPLE_RATE));
    }
}

// Сколько колонка терпит отсутствие связи, прежде чем перезагрузиться сама.
// Клиент WebSocket переподключается сам, но иногда стек залипает так, что
// переподключение не помогает: колонка молчит и на слово, и на кнопку, и
// оживает только выдёргиванием питания. Перезагрузка дешевле такого молчания.
#define WATCHDOG_TIMEOUT_MS (90 * 1000)
#define WATCHDOG_PERIOD_MS 5000
// Через сколько пробовать поднять соединение, не перезагружая плату.
#define RESTART_LINK_MS (20 * 1000)

static void watchdog_task(void *arg)
{
    TickType_t offline_since = xTaskGetTickCount();
    bool restart_tried = false;

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(WATCHDOG_PERIOD_MS));

        if (happy_ws_connected()) {
            offline_since = xTaskGetTickCount();
            restart_tried = false;
            continue;
        }
        TickType_t offline_for = xTaskGetTickCount() - offline_since;

        // Сначала пробуем поднять соединение заново: клиент часто застревает
        // сам по себе, а перезагрузка ради этого — слишком грубо, она стирает
        // и разговор, и прогретые буферы.
        if (offline_for > pdMS_TO_TICKS(RESTART_LINK_MS) && !restart_tried) {
            restart_tried = true;
            happy_ws_restart();
            continue;
        }
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
    // Кодирование/декодирование Opus вынесено в свою задачу с низким
    // приоритетом (audio_opus.c) — первая версия звала кодек синхронно из
    // afe_fetch/websocket_task и валила их тайминги. Неудача не смертельна —
    // send_hello() сам попросит PCM, если Opus не поднялся.
    happy_opus_init();
    ESP_ERROR_CHECK(happy_wake_cache_start());
    ESP_ERROR_CHECK(happy_audio_debug_start());
    // Обработку поднимаем до микрофона: он сразу начнёт гнать через неё звук.
    ESP_ERROR_CHECK(happy_frontend_start(send_mic_frame, on_wake_word));
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
