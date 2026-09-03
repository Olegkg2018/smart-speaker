#include "app.h"

#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "wake_cache";

// Кольцевой буфер звука, который копится ДО срабатывания активационного
// слова. Приём взят из wake_words/wake_word_audio_cache.* в
// 78/xiaozhi-esp32.
//
// Зачем: WakeNet сообщает о слове уже после того, как оно произнесено, и
// запись у нас начиналась только в этот момент. Всё, что человек успел
// сказать раньше, пропадало — в первую очередь конец самого слова и
// первый слог команды, если говорить слитно: «компьютер, включи музыку»
// превращалось в «ключи музыку». Теперь микрофон пишется в кольцо всегда,
// и в момент срабатывания накопленное уходит на сервер первым.
//
// Буфер в PSRAM: внутренней памяти на плате около четверти мегабайта, и
// она уже поделена со звуковым буфером динамика.

static int16_t *s_buf;
static size_t s_capacity;   // всего сэмплов
static size_t s_filled;     // сколько реально накоплено
static size_t s_write;      // куда писать следующий
static SemaphoreHandle_t s_lock;

esp_err_t happy_wake_cache_start(void)
{
    s_capacity = (size_t)HAPPY_MIC_SAMPLE_RATE * CONFIG_HAPPY_WAKE_CACHE_MS / 1000;
    if (s_capacity == 0) {
        return ESP_OK;
    }

    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
        ESP_LOGW(TAG, "не создался замок, буфер перед словом выключен");
        s_capacity = 0;
        return ESP_OK;
    }

    s_buf = heap_caps_malloc(s_capacity * sizeof(int16_t),
                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_buf == NULL) {
        // Не смертельно: колонка работает и без него, просто теряет начало
        // команды, как раньше.
        ESP_LOGW(TAG, "не хватило PSRAM на %d мс звука — буфер выключен",
                 CONFIG_HAPPY_WAKE_CACHE_MS);
        vSemaphoreDelete(s_lock);
        s_lock = NULL;
        s_capacity = 0;
        return ESP_OK;
    }

    ESP_LOGI(TAG, "копится %d мс звука до активационного слова (%u КБ в PSRAM)",
             CONFIG_HAPPY_WAKE_CACHE_MS,
             (unsigned)(s_capacity * sizeof(int16_t) / 1024));
    return ESP_OK;
}

void happy_wake_cache_store(const int16_t *pcm, size_t samples)
{
    if (s_buf == NULL || pcm == NULL || samples == 0) {
        return;
    }
    // Кадр длиннее кольца — оставляем только его хвост, старое всё равно
    // будет затёрто.
    if (samples > s_capacity) {
        pcm += samples - s_capacity;
        samples = s_capacity;
    }

    if (xSemaphoreTake(s_lock, 0) != pdTRUE) {
        return;  // читатель занят; терять кадр диагностики не страшно
    }
    size_t tail = s_capacity - s_write;
    if (samples <= tail) {
        memcpy(s_buf + s_write, pcm, samples * sizeof(int16_t));
    } else {
        memcpy(s_buf + s_write, pcm, tail * sizeof(int16_t));
        memcpy(s_buf, pcm + tail, (samples - tail) * sizeof(int16_t));
    }
    s_write = (s_write + samples) % s_capacity;
    if (s_filled < s_capacity) {
        s_filled += samples;
        if (s_filled > s_capacity) {
            s_filled = s_capacity;
        }
    }
    xSemaphoreGive(s_lock);
}

size_t happy_wake_cache_drain(happy_wake_cache_cb_t on_chunk)
{
    if (s_buf == NULL || on_chunk == NULL) {
        return 0;
    }
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) != pdTRUE) {
        return 0;
    }

    size_t total = s_filled;
    size_t start = (s_write + s_capacity - total) % s_capacity;

    // Отдаём кусками того же размера, что и обычный кадр микрофона: на той
    // стороне ничего не меняется, это просто звук, пришедший чуть раньше.
    static int16_t chunk[HAPPY_MIC_FRAME_SAMPLES];
    size_t sent = 0;
    while (sent < total) {
        size_t n = total - sent;
        if (n > HAPPY_MIC_FRAME_SAMPLES) {
            n = HAPPY_MIC_FRAME_SAMPLES;
        }
        for (size_t i = 0; i < n; i++) {
            chunk[i] = s_buf[(start + sent + i) % s_capacity];
        }
        on_chunk(chunk, n);
        sent += n;
    }

    // Отданное больше не наше: иначе следующая активация пришлёт то же
    // самое вторым слоем.
    s_filled = 0;
    s_write = 0;
    xSemaphoreGive(s_lock);
    return sent;
}

void happy_wake_cache_clear(void)
{
    if (s_buf == NULL || xSemaphoreTake(s_lock, 0) != pdTRUE) {
        return;
    }
    s_filled = 0;
    s_write = 0;
    xSemaphoreGive(s_lock);
}
