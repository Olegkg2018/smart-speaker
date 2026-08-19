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
// речи выйдут с заиканием.
#define PREBUFFER_BYTES (BYTES_PER_MS * 120)
// Сколько ждать продолжения, прежде чем считать поток законченным.
// Без этой паузы любой просвет между порциями звука от облака обрывал
// воспроизведение и требовал полного прогрева заново — на слух это
// и есть «прерывистый разговор».
#define STARVE_GRACE_MS 400

static i2s_chan_handle_t s_tx;
static StreamBufferHandle_t s_ring;
static bool s_prebuffering = true;

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
        size_t written = 0;
        i2s_channel_write(s_tx, chunk, got, &written, pdMS_TO_TICKS(200));
    }
}

esp_err_t happy_audio_out_start(void)
{
    s_ring = xStreamBufferCreate(RING_BYTES, 1);
    if (s_ring == NULL) {
        ESP_LOGE(TAG, "не хватило памяти на буфер вывода");
        return ESP_ERR_NO_MEM;
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
}
