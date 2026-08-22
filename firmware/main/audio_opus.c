#include "app.h"

#include <string.h>

#include "esp_log.h"
#include "esp_opus_dec.h"
#include "esp_opus_enc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

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

// Первая версия звала кодек синхронно прямо из afe_fetch (кодирование) и из
// задачи WebSocket-клиента (декодирование) — у обеих есть жёсткий тайминг
// (эхоподавитель и активационное слово; поддержание сокета), и Opus обе
// проваливал: то переполнением стека, то обрывом соединения ровно в момент
// пробуждения, потому что энкодер оказался куда тяжелее по стеку, чем
// казалось (реальный пик — около 22,6 КБ, измерено на плате через
// uxTaskGetStackHighWaterMark). У 78/xiaozhi-esp32 (main/audio/
// audio_service.cc) кодек живёт в своей задаче с САМЫМ низким приоритетом
// и стеком 24 КБ — то же число, независимо. Здесь так же: если кодек
// отстанет, подрастёт очередь и добавится задержка звука, а не упадёт
// что-то срочное.
//
// На практике приоритет 3 оказался слишком низким: на живом тесте очередь
// кодирования переполнялась ~4,5 секунды подряд сразу после пробуждения —
// AEC/WakeNet (afe_fetch, приоритет 5) и приём кадров (сеть, приоритет 5)
// настолько заняты обоими ядрами, что задаче кодека не доставалось времени
// вовсе, а не просто отставание. Поднято до одного уровня с ними: очередь
// всё равно ограничивает бэклог (кадр теряется, а не копится бесконечно),
// но конкурировать за CPU наравне с остальным звуком кодек теперь может.
#define OPUS_TASK_STACK 24576
#define OPUS_TASK_PRIORITY 5
#define ENCODE_QUEUE_LEN 4
#define DECODE_QUEUE_LEN 4

typedef struct {
    int16_t samples[HAPPY_MIC_MAX_FRAME_SAMPLES];
    size_t count;
} encode_item_t;

typedef struct {
    uint8_t data[OPUS_MAX_PACKET_BYTES];
    size_t len;
} decode_item_t;

static void *s_encoder;
static void *s_decoder;
static QueueHandle_t s_encode_queue;
static QueueHandle_t s_decode_queue;
static QueueSetHandle_t s_queue_set;

// Кадр AFE (512 сэмплов на ESP32-S3) и кадр Opus (320 сэмплов = 20 мс на
// 16 кГц) не кратны друг другу — копим остаток между вызовами, как сервер
// копит микрофон перед отправкой в Realtime (тот же принцип, другая сторона
// провода). Живёт только в задаче кодека — гонок с остальными нет.
#define ENC_FRAME_SAMPLES HAPPY_MIC_FRAME_SAMPLES
static int16_t s_enc_acc[ENC_FRAME_SAMPLES * 2];
static size_t s_enc_acc_len;

static void process_encode(const encode_item_t *item)
{
    size_t offset = 0;
    while (offset < item->count) {
        size_t take = ENC_FRAME_SAMPLES - s_enc_acc_len;
        if (take > item->count - offset) {
            take = item->count - offset;
        }
        memcpy(s_enc_acc + s_enc_acc_len, item->samples + offset, take * sizeof(int16_t));
        s_enc_acc_len += take;
        offset += take;

        if (s_enc_acc_len < ENC_FRAME_SAMPLES) {
            break;  // на целый кадр Opus ещё не набралось
        }

        static uint8_t out_buf[OPUS_MAX_PACKET_BYTES];
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
        happy_ws_send_mic_raw(out_buf, out.encoded_bytes);
    }
}

static void process_decode(const decode_item_t *item)
{
    static int16_t pcm[HAPPY_SPK_FRAME_SAMPLES];
    esp_audio_dec_in_raw_t raw = {
        .buffer = (uint8_t *)item->data,
        .len = item->len,
    };
    esp_audio_dec_out_frame_t frame = {
        .buffer = (uint8_t *)pcm,
        .len = sizeof(pcm),
    };
    esp_audio_dec_info_t info;
    esp_audio_err_t ret = esp_opus_dec_decode(s_decoder, &raw, &frame, &info);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "декодирование Opus не удалось: %d", ret);
        return;
    }
    happy_audio_out_push((const uint8_t *)pcm, frame.decoded_size);
}

static void opus_task(void *arg)
{
    while (true) {
        QueueSetMemberHandle_t ready = xQueueSelectFromSet(s_queue_set, portMAX_DELAY);
        if (ready == s_encode_queue) {
            encode_item_t item;
            if (xQueueReceive(s_encode_queue, &item, 0) == pdTRUE) {
                process_encode(&item);
            }
        } else if (ready == s_decode_queue) {
            decode_item_t item;
            if (xQueueReceive(s_decode_queue, &item, 0) == pdTRUE) {
                process_decode(&item);
            }
        }
    }
}

static void close_codecs(void)
{
    if (s_encoder != NULL) {
        esp_opus_enc_close(s_encoder);
        s_encoder = NULL;
    }
    if (s_decoder != NULL) {
        esp_opus_dec_close(s_decoder);
        s_decoder = NULL;
    }
}

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
        close_codecs();
        return ESP_FAIL;
    }

    s_encode_queue = xQueueCreate(ENCODE_QUEUE_LEN, sizeof(encode_item_t));
    s_decode_queue = xQueueCreate(DECODE_QUEUE_LEN, sizeof(decode_item_t));
    s_queue_set = xQueueCreateSet(ENCODE_QUEUE_LEN + DECODE_QUEUE_LEN);
    if (s_encode_queue == NULL || s_decode_queue == NULL || s_queue_set == NULL ||
        xQueueAddToSet(s_encode_queue, s_queue_set) != pdPASS ||
        xQueueAddToSet(s_decode_queue, s_queue_set) != pdPASS) {
        ESP_LOGW(TAG, "не удалось поднять очереди Opus — останусь на PCM");
        close_codecs();
        return ESP_FAIL;
    }

    if (xTaskCreate(opus_task, "opus_codec", OPUS_TASK_STACK, NULL, OPUS_TASK_PRIORITY, NULL) != pdPASS) {
        ESP_LOGW(TAG, "не запустилась задача кодека Opus — останусь на PCM");
        close_codecs();
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Opus готов: %d бит/с, кадр 20 мс, задача с приоритетом %d",
             OPUS_BITRATE, OPUS_TASK_PRIORITY);
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

void happy_opus_submit_encode(const int16_t *pcm, size_t samples)
{
    if (s_encoder == NULL) {
        return;
    }
    if (samples > HAPPY_MIC_MAX_FRAME_SAMPLES) {
        samples = HAPPY_MIC_MAX_FRAME_SAMPLES;  // не должно случаться, но не переполнять буфер
    }
    encode_item_t item;
    memcpy(item.samples, pcm, samples * sizeof(int16_t));
    item.count = samples;
    if (xQueueSend(s_encode_queue, &item, 0) != pdTRUE) {
        ESP_LOGW(TAG, "очередь кодирования Opus переполнена — кадр потерян");
    }
}

void happy_opus_submit_decode(const uint8_t *packet, size_t packet_len)
{
    if (s_decoder == NULL) {
        return;
    }
    if (packet_len > OPUS_MAX_PACKET_BYTES) {
        ESP_LOGW(TAG, "пакет Opus крупнее буфера: %u байт", (unsigned)packet_len);
        return;
    }
    decode_item_t item;
    memcpy(item.data, packet, packet_len);
    item.len = packet_len;
    if (xQueueSend(s_decode_queue, &item, 0) != pdTRUE) {
        ESP_LOGW(TAG, "очередь декодирования Opus переполнена — кадр потерян");
    }
}
