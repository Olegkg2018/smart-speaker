#include "app.h"

#include <string.h>

#include "esp_afe_aec.h"
#include "esp_agc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

static const char *TAG = "frontend";

// Эхоподавитель сравнивает микрофон с тем, что колонка играет сама, и
// вычитает совпадающее. Без него микрофон слышит собственный динамик:
// колонка выполняла команды из играющей песни и не могла слушать во время
// своего ответа. «MR» — один канал микрофона и один опорный, ровно наша
// схема INMP441 плюс MAX98357A.
#define AEC_FORMAT "MR"
// Длина фильтра в кадрах по 16 мс. Четыре — это примерно 64 мс эха: с
// запасом на путь «динамик — стены комнаты — микрофон», но без лишней
// нагрузки на процессор.
#define AEC_FILTER_LENGTH 4

// WebRTC AGC принимает строго кадры по 10 мс.
#define AGC_FRAME_SAMPLES (HAPPY_MIC_SAMPLE_RATE / 100)
// Целевой уровень громкости, дБ ниже максимума. Ближе к нулю — громче, но
// растёт риск перегрузки на близком голосе.
#define AGC_TARGET_DBFS 3
#define AGC_GAIN_DB 12

static afe_aec_handle_t *s_aec;
static void *s_agc;
static int s_chunk;             // сколько сэмплов за раз ждёт эхоподавитель
static int16_t *s_interleaved;  // [мик, опорный, мик, опорный, …]
static int16_t *s_clean;        // выход эхоподавителя
static int16_t *s_pending;      // микрофон, не набравший полный chunk
static size_t s_pending_len;

bool happy_frontend_available(void)
{
    return s_aec != NULL;
}

esp_err_t happy_frontend_start(void)
{
    s_aec = afe_aec_create(AEC_FORMAT, AEC_FILTER_LENGTH, AFE_TYPE_FD, AFE_MODE_HIGH_PERF);
    if (s_aec == NULL) {
        // Колонка обязана работать и без эхоподавления — просто будет
        // слышать саму себя, как раньше.
        ESP_LOGW(TAG, "эхоподавитель не поднялся — работаю без него");
        return ESP_OK;
    }

    s_chunk = afe_aec_get_chunksize(s_aec);
    // Буферы просим в PSRAM: на внутреннюю память и без того тесно, а
    // выравнивание нужно самому эхоподавителю.
    s_interleaved = heap_caps_aligned_alloc(16, s_chunk * 2 * sizeof(int16_t),
                                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_clean = heap_caps_aligned_alloc(16, s_chunk * sizeof(int16_t),
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_pending = heap_caps_aligned_alloc(16, s_chunk * sizeof(int16_t),
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_interleaved == NULL || s_clean == NULL || s_pending == NULL) {
        ESP_LOGW(TAG, "не хватило памяти на эхоподавление — работаю без него");
        happy_frontend_stop();
        return ESP_OK;
    }

    // AGC_MODE_2 — цифровая регулировка: подтягивает тихий далёкий голос и
    // придерживает слишком громкий вблизи. Именно из-за его отсутствия
    // колонка слышала только вплотную к микрофону.
    s_agc = esp_agc_open(AGC_MODE_2, HAPPY_MIC_SAMPLE_RATE);
    if (s_agc != NULL) {
        set_agc_config(s_agc, AGC_GAIN_DB, 1, AGC_TARGET_DBFS);
    } else {
        ESP_LOGW(TAG, "автоусиление не поднялось — громкость останется как есть");
    }

    ESP_LOGI(TAG, "эхоподавление и автоусиление включены (кадр %d сэмплов)", s_chunk);
    return ESP_OK;
}

void happy_frontend_stop(void)
{
    if (s_aec != NULL) {
        afe_aec_destroy(s_aec);
        s_aec = NULL;
    }
    if (s_agc != NULL) {
        esp_agc_close(s_agc);
        s_agc = NULL;
    }
    heap_caps_free(s_interleaved);
    heap_caps_free(s_clean);
    heap_caps_free(s_pending);
    s_interleaved = s_clean = s_pending = NULL;
    s_pending_len = 0;
}

static void apply_agc(int16_t *pcm, size_t samples)
{
    if (s_agc == NULL) {
        return;
    }
    // Кадр эхоподавителя обычно кратен десяти миллисекундам, но хвост
    // короче кадра AGC оставляем как есть: усиливать его отдельно нельзя,
    // а терять — значит рвать речь.
    for (size_t i = 0; i + AGC_FRAME_SAMPLES <= samples; i += AGC_FRAME_SAMPLES) {
        esp_agc_process(s_agc, pcm + i, pcm + i, AGC_FRAME_SAMPLES, HAPPY_MIC_SAMPLE_RATE);
    }
}

void happy_frontend_process(const int16_t *mic, size_t samples, happy_frontend_cb_t on_clean)
{
    if (s_aec == NULL) {
        on_clean(mic, samples);  // без эхоподавления отдаём как есть
        return;
    }

    while (samples > 0) {
        size_t need = s_chunk - s_pending_len;
        size_t take = samples < need ? samples : need;
        memcpy(s_pending + s_pending_len, mic, take * sizeof(int16_t));
        s_pending_len += take;
        mic += take;
        samples -= take;

        if (s_pending_len < (size_t)s_chunk) {
            return;  // ждём, пока наберётся полный кадр
        }

        // Опорный сигнал берём столько, сколько накопилось: если колонка
        // молчит, его нет вовсе — тогда вычитать нечего и подставляем тишину.
        static int16_t ref[512];
        size_t ref_len = 0;
        if ((size_t)s_chunk <= sizeof(ref) / sizeof(ref[0])) {
            ref_len = happy_audio_out_take_reference(ref, s_chunk);
        }
        memset(ref + ref_len, 0, (s_chunk - ref_len) * sizeof(int16_t));

        for (int i = 0; i < s_chunk; i++) {
            s_interleaved[i * 2] = s_pending[i];
            s_interleaved[i * 2 + 1] = ref[i];
        }
        s_pending_len = 0;

        afe_aec_process(s_aec, s_interleaved, s_clean);
        apply_agc(s_clean, s_chunk);
        on_clean(s_clean, s_chunk);
    }
}
