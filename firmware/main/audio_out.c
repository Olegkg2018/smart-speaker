#include "app.h"

#include <string.h>

#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

static const char *TAG = "audio_out";

#define BYTES_PER_MS (HAPPY_SPK_SAMPLE_RATE * 2 / 1000)  // 96 байт/мс
// Буфер на треть секунды: столько нужно, чтобы пережить типичный всплеск
// задержки Wi-Fi, не съедая при этом слишком много ОЗУ.
#define RING_BYTES (BYTES_PER_MS * 340)
// Пока не накопится этот запас, в I2S не пишем: иначе первые же миллисекунды
// речи выйдут с заиканием. Двести миллисекунд — компромисс: облако отдаёт
// начало реплики неровно, и меньшего запаса не хватало, речь спотыкалась
// на первых словах. Задержка старта на слух почти незаметна.
#define PREBUFFER_BYTES (BYTES_PER_MS * 200)
// Сколько ждать продолжения, прежде чем считать поток законченным.
// Без этой паузы любой просвет между порциями звука от облака обрывал
// воспроизведение и требовал полного прогрева заново — на слух это
// и есть «прерывистый разговор».
#define STARVE_GRACE_MS 400

static i2s_chan_handle_t s_tx;
static StreamBufferHandle_t s_ring;
static bool s_prebuffering = true;

// Опорный сигнал для эхоподавления: то же, что уходит в динамик, но
// прорежённое до частоты микрофона. Эхоподавитель сравнивает его с тем, что
// слышит микрофон, и вычитает — иначе колонка реагирует на собственную речь
// и музыку. Полсекунды с запасом: больше задержки тракта «динамик — воздух —
// микрофон», меньше заметного расхода памяти.
#define REF_RATIO (HAPPY_SPK_SAMPLE_RATE / HAPPY_MIC_SAMPLE_RATE)  // 48к → 16к
#define REF_RING_SAMPLES (HAPPY_MIC_SAMPLE_RATE / 2)
static StreamBufferHandle_t s_ref_ring;

static esp_err_t init_i2s(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = HAPPY_SPK_FRAME_SAMPLES / 2;
    chan_cfg.auto_clear = true;  // при недоборе данных выдаёт тишину, а не треск
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx, NULL));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(HAPPY_SPK_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CONFIG_HAPPY_I2S_SPK_BCLK,
            .ws = CONFIG_HAPPY_I2S_SPK_WS,
            .dout = CONFIG_HAPPY_I2S_SPK_DOUT,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std_cfg));
    return i2s_channel_enable(s_tx);
}

static void push_reference(const uint8_t *pcm, size_t len);

static void speaker_task(void *arg)
{
    static uint8_t chunk[HAPPY_SPK_FRAME_SAMPLES * 2];
    TickType_t starving_since = 0;

    while (true) {
        size_t available = xStreamBufferBytesAvailable(s_ring);

        if (s_prebuffering) {
            if (available < PREBUFFER_BYTES) {
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
            s_prebuffering = false;
            starving_since = 0;
        }
        if (available == 0) {
            // Просвет в потоке — ещё не конец реплики. Ждём продолжения:
            // I2S в это время сам выдаёт тишину (auto_clear), а мы не сбиваем
            // воспроизведение на повторный прогрев из-за каждой заминки сети.
            TickType_t now = xTaskGetTickCount();
            if (starving_since == 0) {
                starving_since = now;
            } else if ((now - starving_since) > pdMS_TO_TICKS(STARVE_GRACE_MS)) {
                s_prebuffering = true;
                starving_since = 0;
            }
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        starving_since = 0;

        size_t got = xStreamBufferReceive(s_ring, chunk, sizeof(chunk), pdMS_TO_TICKS(50));
        if (got == 0) {
            continue;
        }
        push_reference(chunk, got);

        size_t written = 0;
        i2s_channel_write(s_tx, chunk, got, &written, pdMS_TO_TICKS(200));
    }
}

// Копию для эхоподавителя снимаем прямо перед записью в I2S, а не при
// приёме кадра: между приёмом и звучанием лежит буфер в треть секунды, и
// опорный сигнал разъехался бы с тем, что слышит микрофон.
static void push_reference(const uint8_t *pcm, size_t len)
{
    if (s_ref_ring == NULL) {
        return;
    }
    const int16_t *src = (const int16_t *)pcm;
    size_t samples = len / 2;
    static int16_t down[HAPPY_SPK_FRAME_SAMPLES / REF_RATIO];
    size_t out = 0;

    // Прореживание с усреднением: простое «брать каждый третий» даёт
    // призвуки, из-за которых эхоподавитель хуже находит эхо.
    for (size_t i = 0; i + REF_RATIO <= samples && out < sizeof(down) / 2; i += REF_RATIO) {
        int32_t sum = 0;
        for (int k = 0; k < REF_RATIO; k++) {
            sum += src[i + k];
        }
        down[out++] = (int16_t)(sum / REF_RATIO);
    }
    if (out == 0) {
        return;
    }
    // Не ждём: опорный сигнал важен, но не ценой заикания динамика.
    // Переполнение значит, что микрофон не забирает — старое всё равно
    // бесполезно, поэтому освобождаем место.
    if (xStreamBufferSpacesAvailable(s_ref_ring) < out * 2) {
        xStreamBufferReset(s_ref_ring);
    }
    xStreamBufferSend(s_ref_ring, down, out * 2, 0);
}

esp_err_t happy_audio_out_start(void)
{
    s_ring = xStreamBufferCreate(RING_BYTES, 1);
    if (s_ring == NULL) {
        ESP_LOGE(TAG, "не хватило памяти на буфер вывода");
        return ESP_ERR_NO_MEM;
    }
    s_ref_ring = xStreamBufferCreate(REF_RING_SAMPLES * 2, 1);
    if (s_ref_ring == NULL) {
        // Без опорного сигнала эхоподавитель работать не сможет, но сама
        // колонка — вполне: продолжаем без него.
        ESP_LOGW(TAG, "не хватило памяти на опорный сигнал — эхоподавление отключено");
    }
    ESP_ERROR_CHECK(init_i2s());
    if (xTaskCreate(speaker_task, "speaker", 4096, NULL, 7, NULL) != pdPASS) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "динамик запущен: %d Гц, буфер %d мс", HAPPY_SPK_SAMPLE_RATE,
             RING_BYTES / BYTES_PER_MS);
    return ESP_OK;
}

void happy_audio_out_push(const uint8_t *pcm, size_t len)
{
    if (s_ring == NULL) {
        return;
    }
    // Не блокируемся: обработчик WebSocket не должен ждать динамик.
    // Переполнение означает, что сервер шлёт быстрее реального времени —
    // лучше потерять кадр, чем застопорить приём.
    size_t sent = xStreamBufferSend(s_ring, pcm, len, 0);
    if (sent < len) {
        ESP_LOGW(TAG, "буфер вывода переполнен, кадр потерян");
    }
}

void happy_audio_out_flush(void)
{
    if (s_ring != NULL) {
        xStreamBufferReset(s_ring);
        s_prebuffering = true;
    }
    // Старое эхо больше не прозвучит — сравнивать микрофон не с чем.
    if (s_ref_ring != NULL) {
        xStreamBufferReset(s_ref_ring);
    }
}

size_t happy_audio_out_take_reference(int16_t *dst, size_t samples)
{
    if (s_ref_ring == NULL) {
        return 0;
    }
    size_t got = xStreamBufferReceive(s_ref_ring, dst, samples * 2, 0);
    return got / 2;
}
