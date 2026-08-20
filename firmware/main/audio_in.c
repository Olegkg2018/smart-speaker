#include "app.h"

#include <string.h>

#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "audio_in";

// INMP441 отдаёт 24 бита в 32-битном слоте и по меркам линейного входа тихий.
// Сдвиг на 14 вместо 16 поднимает уровень примерно вчетверо — на слух
// получается нормальная громкость речи с расстояния метра-двух.
// Если звук клиппит, увеличьте сдвиг; если тихо — уменьшите.
#define MIC_SHIFT 14

static i2s_chan_handle_t s_rx;
static volatile bool s_recording;

static esp_err_t init_i2s(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 4;
    chan_cfg.dma_frame_num = HAPPY_MIC_FRAME_SAMPLES;
    chan_cfg.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &s_rx));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(HAPPY_MIC_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CONFIG_HAPPY_I2S_MIC_BCLK,
            .ws = CONFIG_HAPPY_I2S_MIC_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = CONFIG_HAPPY_I2S_MIC_DIN,
            .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
        },
    };
    // INMP441 с SEL на землю говорит в левый слот.
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &std_cfg));
    return i2s_channel_enable(s_rx);
}

static void mic_task(void *arg)
{
    static int32_t raw[HAPPY_MIC_FRAME_SAMPLES];
    static int16_t pcm[HAPPY_MIC_FRAME_SAMPLES];

    while (true) {
        size_t read = 0;
        esp_err_t err = i2s_channel_read(s_rx, raw, sizeof(raw), &read, pdMS_TO_TICKS(200));
        if (err != ESP_OK || read == 0) {
            continue;
        }
        // Обрабатываем всегда, даже когда не пишем: активационное слово
        // ищется здесь же, и без постоянного потока его не услышать.
        // Отправкой на сервер занимается колбэк — он и смотрит на s_recording.

        const size_t samples = read / sizeof(int32_t);
        for (size_t i = 0; i < samples; i++) {
            int32_t value = raw[i] >> MIC_SHIFT;
            if (value > INT16_MAX) value = INT16_MAX;
            if (value < INT16_MIN) value = INT16_MIN;
            pcm[i] = (int16_t)value;
        }
        // Эхоподавитель вычитает то, что играет сама колонка, автоусиление
        // подтягивает далёкий голос. Кадры у него своей длины, поэтому
        // отправкой занимается колбэк, а не этот цикл.
        happy_frontend_process(pcm, samples);
    }
}

esp_err_t happy_audio_in_start(void)
{
    ESP_ERROR_CHECK(init_i2s());
    if (xTaskCreate(mic_task, "mic", 4096, NULL, 6, NULL) != pdPASS) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "микрофон запущен: %d Гц", HAPPY_MIC_SAMPLE_RATE);
    return ESP_OK;
}

void happy_audio_in_set_recording(bool recording)
{
    s_recording = recording;
}

bool happy_audio_in_is_recording(void)
{
    return s_recording;
}
