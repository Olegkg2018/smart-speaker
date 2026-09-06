#include "app.h"

#include <string.h>

#include "driver/i2s_std.h"
#include "esp_agc.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "audio_in";

// INMP441 отдаёт 24 бита в 32-битном слоте и по меркам линейного входа тихий.
// Было 14 (речь на слух нормальной громкости с метра-двух), потом 12
// (активационное слово перестало требовать крика) — подтверждено живьём
// как рабочее значение. Пробовали 10 (замер тихой тестовой фразой через
// audio_debug.c показывал ещё нулевой клиппинг) — но на настоящем,
// более громком/эмоциональном разговоре Whisper начал выдавать полную
// тарабарщину на случайных языках («Danke für die Sache», деванагари,
// английские обрывки) вместо реальных слов: тестовая фраза была
// спокойнее, чем обычная речь, и не поймала клиппинг, который реально
// происходит на пиках погромче. Откачено обратно на 12 — единственное
// значение, подтверждённое на живом разговоре без этого эффекта.
#define MIC_SHIFT 12

// Собственный AGC (WebRTC, из той же библиотеки esp-sr) — в отличие от
// agc_init во фронтенде (audio_frontend.c), который стоит ПОСЛЕ WakeNet в
// конвейере (AEC -> WakeNet -> AGC, см. лог загрузки), этот применяется
// прямо тут, ДО фронтенда: активационное слово и распознавание команды
// после него получают уже выровненный по громкости сигнал, а не голый.
// Статичного MIC_SHIFT недостаточно — комфортно ни для громкого голоса
// вплотную (клиппинг), ни для тихого издалека (не слышно) сразу.
#define AGC_FRAME_SAMPLES (HAPPY_MIC_SAMPLE_RATE / 100)  // 10 мс — так хочет esp_agc_process

static i2s_chan_handle_t s_rx;
static volatile bool s_recording;
static void *s_agc;

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
    static int16_t leveled[HAPPY_MIC_FRAME_SAMPLES];

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

        const int16_t *out = pcm;
        if (s_agc != NULL) {
            // esp_agc_process ждёt кадры ровно по 10 мс — режем на куски,
            // хвост меньше 10 мс (бывает при неполном чтении DMA) просто
            // копируем как есть, чтобы не потерять его вовсе.
            size_t off = 0;
            for (; off + AGC_FRAME_SAMPLES <= samples; off += AGC_FRAME_SAMPLES) {
                esp_agc_process(s_agc, (int16_t *)pcm + off, leveled + off,
                                 AGC_FRAME_SAMPLES, HAPPY_MIC_SAMPLE_RATE);
            }
            if (off < samples) {
                memcpy(leveled + off, pcm + off, (samples - off) * sizeof(int16_t));
            }
            out = leveled;
        }

        // Эхоподавитель вычитает то, что играет сама колонка. Кадры у него
        // своей длины, поэтому отправкой занимается колбэк, а не этот цикл.
        happy_frontend_process(out, samples);
    }
}

esp_err_t happy_audio_in_start(void)
{
    ESP_ERROR_CHECK(init_i2s());

    // AGC_MODE_2 — цифровой AGC (в отличие от AGC_MODE_1, не эмулирует
    // аналоговый тракт, предсказуемее на цифровом входе вроде нашего).
    // target_level_dbfs -3 — библиотечное значение по умолчанию, ближе к
    // максимуму без клиппинга: чем меньше запас, тем лучше отношение
    // сигнал/шум для WakeNet и распознавания. limiter_enable=1 обязателен —
    // это единственная защита от клиппинга ПОСЛЕ усиления (то, что уже
    // клиппировано до AGC самим MIC_SHIFT, он не восстановит).
    //
    // gain_dB=0 — пробовали поднять до 30 в надежде, что AGC станет
    // усиливать заметнее (замер показывал, что голос не доходит выше
    // -19 dBFS при цели -3), но в тесте эффекта не дало вовсе (числа
    // совпали с gain=0 день в день), а живьём не проверялось. Раз в этот
    // же день ошиблись дважды подряд с гипотезами о причине (сначала
    // gain_dB, потом MIC_SHIFT=10 — оба раза откатывали), не гадаем в
    // третий раз: 0 — единственное значение, прожитое несколько дней
    // реальных разговоров без явной регрессии распознавания.
    s_agc = esp_agc_open(AGC_MODE_2, HAPPY_MIC_SAMPLE_RATE);
    if (s_agc != NULL) {
        set_agc_config(s_agc, /*gain_dB=*/0, /*limiter_enable=*/1, /*target_level_dbfs=*/-3);
    } else {
        ESP_LOGW(TAG, "AGC не поднялся — работаю с фиксированным усилением");
    }

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
