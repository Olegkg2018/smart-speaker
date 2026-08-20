#include "app.h"

#include <string.h>

#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_process_sdkconfig.h"
#include "model_path.h"

static const char *TAG = "frontend";

// Звуковой фронтенд Espressif делает разом три вещи, каждая из которых
// раньше была отдельной болью:
//
//   эхоподавление — микрофон перестаёт слышать собственный динамик, из-за
//     которого колонка выполняла команды из играющей песни;
//   автоусиление — далёкий голос слышно так же, как вплотную;
//   активационное слово — ищется прямо здесь, а не на сервере, поэтому в
//     Wi-Fi больше не течёт круглосуточный поток и плата не занята Vosk.
//
// «MR» — один канал микрофона и один опорный: наша схема INMP441 плюс
// MAX98357A. Опорный сигнал берётся из audio_out.
#define AFE_FORMAT "MR"

static const esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t *s_data;
static srmodel_list_t *s_models;
static int s_feed_chunk;        // сколько сэмплов за раз ждёт фронтенд
static int16_t *s_interleaved;  // [мик, опорный, мик, опорный, …]
static int16_t *s_pending;      // микрофон, не набравший полный кадр
static size_t s_pending_len;
static happy_wake_cb_t s_on_wake;
static happy_frontend_cb_t s_on_clean;
static volatile bool s_wake_enabled = true;

bool happy_frontend_available(void)
{
    return s_data != NULL;
}

void happy_frontend_set_wake_enabled(bool enabled)
{
    s_wake_enabled = enabled;
}

// Забирает обработанный звук. Отдельная задача — так требует сам фронтенд:
// feed кормит его входом, fetch забирает результат, и смешивать нельзя.
static void fetch_task(void *arg)
{
    while (true) {
        afe_fetch_result_t *res = s_afe->fetch(s_data);
        if (res == NULL || res->ret_value == ESP_FAIL) {
            continue;
        }
        if (res->wakeup_state == WAKENET_DETECTED && s_wake_enabled && s_on_wake != NULL) {
            ESP_LOGI(TAG, "услышал активационное слово");
            s_on_wake();
        }
        if (s_on_clean != NULL && res->data != NULL && res->data_size > 0) {
            s_on_clean(res->data, res->data_size / sizeof(int16_t));
        }
    }
}

esp_err_t happy_frontend_start(happy_frontend_cb_t on_clean, happy_wake_cb_t on_wake)
{
    s_on_clean = on_clean;
    s_on_wake = on_wake;

    s_models = esp_srmodel_init("model");
    afe_config_t *cfg = afe_config_init(AFE_FORMAT, s_models, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    if (cfg == NULL) {
        ESP_LOGW(TAG, "фронтенд не настроился — работаю без обработки звука");
        return ESP_OK;
    }
    // Всё это ради одного: колонка должна слышать человека, а не себя.
    cfg->aec_init = true;   // вычесть собственный динамик
    cfg->agc_init = true;   // подтянуть далёкий голос
    cfg->se_init = true;    // подавить шум
    cfg->vad_init = false;  // конец реплики определяет сервер, ему виднее

    s_afe = esp_afe_handle_from_config(cfg);
    s_data = s_afe->create_from_config(cfg);
    if (s_data == NULL) {
        ESP_LOGW(TAG, "фронтенд не поднялся — работаю без обработки звука");
        return ESP_OK;
    }

    s_feed_chunk = s_afe->get_feed_chunksize(s_data);
    int channels = s_afe->get_channel_num(s_data);
    // Буферы в PSRAM: внутренней памяти и без того впритык.
    s_interleaved = heap_caps_aligned_alloc(16, s_feed_chunk * 2 * sizeof(int16_t),
                                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_pending = heap_caps_aligned_alloc(16, s_feed_chunk * sizeof(int16_t),
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_interleaved == NULL || s_pending == NULL) {
        ESP_LOGW(TAG, "не хватило памяти на обработку звука");
        happy_frontend_stop();
        return ESP_OK;
    }

    if (xTaskCreate(fetch_task, "afe_fetch", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGW(TAG, "не запустилась задача разбора звука");
        happy_frontend_stop();
        return ESP_OK;
    }

    char *wake = esp_srmodel_filter(s_models, ESP_WN_PREFIX, NULL);
    ESP_LOGI(TAG, "звук обрабатывается на плате: кадр %d сэмплов, каналов %d, слово «%s»",
             s_feed_chunk, channels, wake ? wake : "нет");
    return ESP_OK;
}

void happy_frontend_stop(void)
{
    if (s_data != NULL && s_afe != NULL) {
        s_afe->destroy(s_data);
        s_data = NULL;
    }
    heap_caps_free(s_interleaved);
    heap_caps_free(s_pending);
    s_interleaved = s_pending = NULL;
    s_pending_len = 0;
}

void happy_frontend_process(const int16_t *mic, size_t samples)
{
    if (s_data == NULL) {
        // Без фронтенда отдаём звук как есть: колонка обязана работать
        // и с необработанным микрофоном, просто хуже.
        if (s_on_clean != NULL) {
            s_on_clean(mic, samples);
        }
        return;
    }

    while (samples > 0) {
        size_t need = s_feed_chunk - s_pending_len;
        size_t take = samples < need ? samples : need;
        memcpy(s_pending + s_pending_len, mic, take * sizeof(int16_t));
        s_pending_len += take;
        mic += take;
        samples -= take;

        if (s_pending_len < (size_t)s_feed_chunk) {
            return;  // ждём полный кадр
        }

        // Опорный сигнал: то, что колонка играет прямо сейчас. Если она
        // молчит, его нет — тогда вычитать нечего, подставляем тишину.
        static int16_t ref[1024];
        size_t ref_len = 0;
        if ((size_t)s_feed_chunk <= sizeof(ref) / sizeof(ref[0])) {
            ref_len = happy_audio_out_take_reference(ref, s_feed_chunk);
            memset(ref + ref_len, 0, (s_feed_chunk - ref_len) * sizeof(int16_t));
        } else {
            memset(ref, 0, sizeof(ref));
        }

        for (int i = 0; i < s_feed_chunk; i++) {
            s_interleaved[i * 2] = s_pending[i];
            s_interleaved[i * 2 + 1] = ref[i];
        }
        s_pending_len = 0;
        s_afe->feed(s_data, s_interleaved);
    }
}
