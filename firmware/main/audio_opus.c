#include "app.h"

#include <string.h>

#include "esp_log.h"
#include "esp_opus_dec.h"
#include "esp_opus_enc.h"

static const char *TAG = "opus";

// Opus экономит трафик примерно в 15 раз против сырого PCM — сервер уже так
// считал (app/audio/codec.py), но прошивка кодек не реализовывала, и сервер
// молча откатывался на PCM. Без сжатия буфер динамика на плате пришлось
// урезать до 700 мс из-за нехватки внутренней памяти (~250 КБ) — со сжатием
// тот же объём памяти даёт секунды, а не доли секунды запаса.
#define OPUS_BITRATE 24000
#define OPUS_COMPLEXITY 5
// С запасом выше типичного размера кадра на этом битрейте (~60 байт).
#define OPUS_MAX_PACKET_BYTES 400

static void *s_encoder;
static void *s_decoder;

// Кадр AFE (512 сэмплов на ESP32-S3) и кадр Opus (320 сэмплов = 20 мс на
// 16 кГц) не кратны друг другу — копим остаток между вызовами, как сервер
// копит микрофон перед отправкой в Realtime (тот же принцип, другая сторона
// провода).
#define ENC_FRAME_SAMPLES HAPPY_MIC_FRAME_SAMPLES
static int16_t s_enc_acc[ENC_FRAME_SAMPLES * 2];
static size_t s_enc_acc_len;

esp_err_t happy_opus_init(void)
{
    esp_opus_enc_config_t enc_cfg = {
        .sample_rate = HAPPY_MIC_SAMPLE_RATE,
        .channel = 1,
        .bits_per_sample = 16,
        .bitrate = OPUS_BITRATE,
        .frame_duration = ESP_OPUS_ENC_FRAME_DURATION_20_MS,
        .application_mode = ESP_OPUS_ENC_APPLICATION_VOIP,
        .complexity = OPUS_COMPLEXITY,
        .enable_fec = false,
        .enable_dtx = false,
        .enable_vbr = false,
    };
    esp_audio_err_t ret = esp_opus_enc_open(&enc_cfg, sizeof(enc_cfg), &s_encoder);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "не удалось открыть кодировщик Opus: %d — останусь на PCM", ret);
        return ESP_FAIL;
    }

    esp_opus_dec_cfg_t dec_cfg = {
        .sample_rate = HAPPY_SPK_SAMPLE_RATE,
        .channel = 1,
        .frame_duration = ESP_OPUS_DEC_FRAME_DURATION_20_MS,
        .self_delimited = false,
    };
    ret = esp_opus_dec_open(&dec_cfg, sizeof(dec_cfg), &s_decoder);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "не удалось открыть декодировщик Opus: %d — останусь на PCM", ret);
        esp_opus_enc_close(s_encoder);
        s_encoder = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Opus готов: %d бит/с, кадр 20 мс", OPUS_BITRATE);
    return ESP_OK;
}

bool happy_opus_available(void)
{
    return s_encoder != NULL && s_decoder != NULL;
}

void happy_opus_reset(void)
{
    s_enc_acc_len = 0;
    if (s_encoder != NULL) {
        esp_opus_enc_reset(s_encoder);
    }
    if (s_decoder != NULL) {
        esp_opus_dec_reset(s_decoder);
    }
}

void happy_opus_encode(const int16_t *pcm, size_t samples, happy_opus_emit_cb_t emit, void *ctx)
{
    static uint8_t out_buf[OPUS_MAX_PACKET_BYTES];

    if (s_encoder == NULL) {
        return;
    }

    size_t offset = 0;
    while (offset < samples) {
        size_t take = ENC_FRAME_SAMPLES - s_enc_acc_len;
        if (take > samples - offset) {
            take = samples - offset;
        }
        memcpy(s_enc_acc + s_enc_acc_len, pcm + offset, take * sizeof(int16_t));
        s_enc_acc_len += take;
        offset += take;

        if (s_enc_acc_len < ENC_FRAME_SAMPLES) {
            break;  // на целый кадр Opus ещё не набралось
        }

        esp_audio_enc_in_frame_t in = {
            .buffer = (uint8_t *)s_enc_acc,
            .len = ENC_FRAME_SAMPLES * sizeof(int16_t),
        };
        esp_audio_enc_out_frame_t out = {
            .buffer = out_buf,
            .len = sizeof(out_buf),
        };
        esp_audio_err_t ret = esp_opus_enc_process(s_encoder, &in, &out);
        s_enc_acc_len = 0;
        if (ret != ESP_AUDIO_ERR_OK) {
            ESP_LOGW(TAG, "кодирование Opus не удалось: %d", ret);
            continue;
        }
        emit(out_buf, out.encoded_bytes, ctx);
    }
}

int happy_opus_decode(const uint8_t *packet, size_t packet_len, int16_t *pcm_out, size_t pcm_out_cap_samples)
{
    if (s_decoder == NULL) {
        return -1;
    }
    esp_audio_dec_in_raw_t raw = {
        .buffer = (uint8_t *)packet,
        .len = packet_len,
    };
    esp_audio_dec_out_frame_t frame = {
        .buffer = (uint8_t *)pcm_out,
        .len = pcm_out_cap_samples * sizeof(int16_t),
    };
    esp_audio_dec_info_t info;
    esp_audio_err_t ret = esp_opus_dec_decode(s_decoder, &raw, &frame, &info);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "декодирование Opus не удалось: %d", ret);
        return -1;
    }
    return (int)(frame.decoded_size / sizeof(int16_t));
}
