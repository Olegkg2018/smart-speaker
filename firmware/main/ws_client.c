#include "app.h"

#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "ws";

// Кадр динамика — 1920 байт PCM плюс байт типа, кадр экрана — 1024 байта.
// Буфер с запасом, чтобы сообщение приходило целиком и не приходилось
// склеивать фрагменты.
#define WS_BUFFER_SIZE 4096

static esp_websocket_client_handle_t s_client;
static volatile bool s_connected;
static volatile happy_state_t s_state = HAPPY_STATE_IDLE;
// Опрашивается в hello, подтверждается сервером в ready — активен только
// когда обе стороны согласились. По умолчанию false: до подтверждения
// безопаснее считать, что идёт PCM, как было раньше этой правки.
static volatile bool s_codec_opus_active;

// Сборка фрагментированных сообщений: esp_websocket_client отдаёт длинные
// пакеты по частям, и тогда payload_offset > 0.
static uint8_t s_assembly[WS_BUFFER_SIZE];
static size_t s_assembly_len;

static void send_hello(void)
{
    char hello[224];
    snprintf(hello, sizeof(hello),
             "{\"t\":\"hello\",\"device\":\"%s\",\"fw\":\"0.1.0\",\"codec\":\"%s\","
             "\"screen\":%s}",
             CONFIG_HAPPY_DEVICE_NAME,
             happy_opus_available() ? "opus" : "pcm",
#if CONFIG_HAPPY_SCREEN_ENABLED
             "true"
#else
             "false"
#endif
    );
    happy_ws_send_json(hello);
}

static happy_state_t parse_state(const char *value)
{
    if (strcmp(value, "listening") == 0) return HAPPY_STATE_LISTENING;
    if (strcmp(value, "thinking") == 0) return HAPPY_STATE_THINKING;
    if (strcmp(value, "speaking") == 0) return HAPPY_STATE_SPEAKING;
    if (strcmp(value, "playing") == 0) return HAPPY_STATE_PLAYING;
    return HAPPY_STATE_IDLE;
}

static void handle_text(const char *data, size_t len)
{
    cJSON *root = cJSON_ParseWithLength(data, len);
    if (root == NULL) {
        ESP_LOGW(TAG, "не разобрал JSON от сервера");
        return;
    }
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "t");
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(root, "value");
    if (cJSON_IsString(type) && strcmp(type->valuestring, "state") == 0 &&
        cJSON_IsString(value)) {
        s_state = parse_state(value->valuestring);
        // В сеть звук уходит только пока сервер слушает реплику.
        // Активационное слово ищет сама плата, поэтому непрерывный поток
        // больше не нужен: он занимал канал, ESP32 не успевала его
        // отдавать и рвала соединение сразу после подключения.
        happy_audio_in_set_recording(s_state == HAPPY_STATE_LISTENING);
        // Сервер перестал говорить — доигрываем остаток и чистим буфер,
        // иначе следующая реплика начнётся с хвоста предыдущей.
        if (s_state == HAPPY_STATE_IDLE) {
            happy_audio_out_flush();
        }
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "ready") == 0) {
        // Сервер подтверждает фактически выбранный кодек — не обязательно
        // тот, что мы попросили в hello: без libopus сервер молча
        // откатывается на PCM, и обе стороны должны узнать про это
        // одинаково, иначе колонка кодирует, а сервер ждёт сырой звук.
        const cJSON *codec = cJSON_GetObjectItemCaseSensitive(root, "codec");
        s_codec_opus_active = happy_opus_available() && cJSON_IsString(codec) &&
                               strcmp(codec->valuestring, "opus") == 0;
        ESP_LOGI(TAG, "кодек согласован: %s", s_codec_opus_active ? "opus" : "pcm");
    }
    cJSON_Delete(root);
}

static void handle_binary(const uint8_t *data, size_t len)
{
    if (len < 2) {
        return;
    }
    switch (data[0]) {
    case HAPPY_FRAME_SPEAKER:
        if (s_codec_opus_active) {
            // Только кладём в очередь — само декодирование идёт в задаче
            // кодека (audio_opus.c), не здесь: у этой задачи есть свой
            // жёсткий дедлайн на обслуживание сокета.
            happy_opus_submit_decode(data + 1, len - 1);
        } else {
            happy_audio_out_push(data + 1, len - 1);
        }
        break;
    case HAPPY_FRAME_SCREEN:
        happy_display_draw(data + 1, len - 1);
        break;
    default:
        break;
    }
}

static void on_ws_event(void *arg, esp_event_base_t base, int32_t id, void *event_data)
{
    esp_websocket_event_data_t *event = (esp_websocket_event_data_t *)event_data;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "соединение с сервером установлено");
        s_connected = true;
        s_assembly_len = 0;
        s_codec_opus_active = false;  // до подтверждения сервером в ready
        happy_opus_reset();
        send_hello();
        // Микрофон включит либо активационное слово, либо кнопка — слать
        // звук до этого некуда и незачем.
        happy_audio_in_set_recording(false);
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "соединение потеряно");
        s_connected = false;
        s_state = HAPPY_STATE_IDLE;
        // Слать некуда — незачем и записывать.
        happy_audio_in_set_recording(false);
        happy_audio_out_flush();
        happy_display_clear();
        break;

    case WEBSOCKET_EVENT_DATA: {
        // op_code 0x8 — close, 0x9/0xA — ping/pong: полезной нагрузки нет.
        if (event->op_code == 0x08 || event->op_code == 0x09 || event->op_code == 0x0A) {
            break;
        }
        if (event->payload_offset == 0 && event->data_len == event->payload_len) {
            // Обычный случай: сообщение пришло целиком.
            if (event->op_code == 0x01) {
                handle_text(event->data_ptr, event->data_len);
            } else {
                handle_binary((const uint8_t *)event->data_ptr, event->data_len);
            }
            break;
        }
        // Фрагментированное сообщение — накапливаем.
        if (event->payload_offset == 0) {
            s_assembly_len = 0;
        }
        if (s_assembly_len + event->data_len > sizeof(s_assembly)) {
            ESP_LOGW(TAG, "сообщение не влезло в буфер, отбрасываю");
            s_assembly_len = 0;
            break;
        }
        memcpy(s_assembly + s_assembly_len, event->data_ptr, event->data_len);
        s_assembly_len += event->data_len;
        if (s_assembly_len >= (size_t)event->payload_len) {
            if (event->op_code == 0x01) {
                handle_text((const char *)s_assembly, s_assembly_len);
            } else {
                handle_binary(s_assembly, s_assembly_len);
            }
            s_assembly_len = 0;
        }
        break;
    }

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "ошибка WebSocket");
        break;

    default:
        break;
    }
}

esp_err_t happy_ws_start(void)
{
    esp_websocket_client_config_t config = {
        .uri = CONFIG_HAPPY_SERVER_URI,
        .buffer_size = WS_BUFFER_SIZE,
        .reconnect_timeout_ms = 2000,
        .network_timeout_ms = 10000,
        // Пинг подтверждает живость соединения, когда никто не говорит.
        .ping_interval_sec = 10,
        // task_stack и task_prio были подняты (8192/7) под декодирование
        // Opus в этой же задаче — сейчас Opus выключен в main.c
        // (happy_opus_init() закомментирован), decode физически не
        // выполняется, а повышенный приоритет только менял тайминг
        // остальных задач без причины. Возврат к прежним рабочим
        // значениям — тем, что были час назад, когда всё было ровно.
        .task_stack = 6144,
    };
    s_client = esp_websocket_client_init(&config);
    if (s_client == NULL) {
        return ESP_FAIL;
    }
    ESP_ERROR_CHECK(esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, on_ws_event, NULL));
    ESP_LOGI(TAG, "подключаюсь к %s", CONFIG_HAPPY_SERVER_URI);
    return esp_websocket_client_start(s_client);
}

bool happy_ws_connected(void)
{
    // Спрашиваем сам клиент, а не свой флаг. Флаг ставится по событиям, а
    // они приходят не всегда: клиент умеет застрять в состоянии «не
    // подключён», продолжая печатать ошибки отправки и не переподключаясь.
    // Сторож при этом считал связь живой и не вмешивался — колонка молчала
    // до выдёргивания питания.
    if (s_client == NULL) {
        return false;
    }
    return s_connected && esp_websocket_client_is_connected(s_client);
}

void happy_ws_restart(void)
{
    if (s_client == NULL) {
        return;
    }
    ESP_LOGW(TAG, "перезапускаю соединение");
    s_connected = false;
    esp_websocket_client_stop(s_client);
    if (esp_websocket_client_start(s_client) != ESP_OK) {
        ESP_LOGE(TAG, "не удалось перезапустить соединение");
    }
}

happy_state_t happy_ws_state(void)
{
    return s_state;
}

// Заголовок и данные должны уйти одним фреймом, поэтому склеиваем. Буфер
// по размеру кадра эхоподавителя (512 сэмплов на ESP32-S3, не 320 у
// микрофона) — Opus-пакет в него тоже помещается с большим запасом. Общий
// статический буфер безопасен: PCM-путь (эта задача) и Opus-путь (задача
// кодека) никогда не активны одновременно — переключает их один и тот же
// флаг s_codec_opus_active.
esp_err_t happy_ws_send_mic_raw(const uint8_t *payload, size_t len)
{
    static uint8_t frame[1 + HAPPY_MIC_MAX_FRAME_SAMPLES * 2];
    if (len + 1 > sizeof(frame)) {
        ESP_LOGW(TAG, "кадр микрофона не влез в буфер: %u байт", (unsigned)len);
        return ESP_ERR_INVALID_SIZE;
    }
    frame[0] = HAPPY_FRAME_MIC;
    memcpy(frame + 1, payload, len);
    int sent = esp_websocket_client_send_bin(s_client, (const char *)frame, len + 1,
                                             pdMS_TO_TICKS(100));
    return sent > 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t happy_ws_send_mic(const uint8_t *payload, size_t len)
{
    if (!s_connected) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_codec_opus_active) {
        // payload — сырой PCM s16le от эхоподавителя. Кодирование и сама
        // отправка идут в задаче кодека (audio_opus.c) — здесь только
        // неблокирующая постановка в очередь, чтобы не держать afe_fetch.
        happy_opus_submit_encode((const int16_t *)payload, len / sizeof(int16_t));
        return ESP_OK;
    }
    return happy_ws_send_mic_raw(payload, len);
}

esp_err_t happy_ws_send_json(const char *json)
{
    if (!s_connected) {
        return ESP_ERR_INVALID_STATE;
    }
    int sent = esp_websocket_client_send_text(s_client, json, strlen(json), pdMS_TO_TICKS(200));
    return sent > 0 ? ESP_OK : ESP_FAIL;
}
