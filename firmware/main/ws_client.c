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

// Сборка фрагментированных сообщений: esp_websocket_client отдаёт длинные
// пакеты по частям, и тогда payload_offset > 0.
static uint8_t s_assembly[WS_BUFFER_SIZE];
static size_t s_assembly_len;

static void send_hello(void)
{
    char hello[224];
    snprintf(hello, sizeof(hello),
             "{\"t\":\"hello\",\"device\":\"%s\",\"fw\":\"0.1.0\",\"codec\":\"pcm\","
             "\"screen\":%s}",
             CONFIG_HAPPY_DEVICE_NAME,
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
        // Микрофон работает почти всегда: активационное слово ищет сервер,
        // и без непрерывного потока ему нечего слушать. Молчим только пока
        // говорит сам ассистент — иначе он услышит собственный голос и
        // примет его за обращение (эхоподавления на плате нет).
        happy_audio_in_set_recording(s_state != HAPPY_STATE_SPEAKING &&
                                     s_state != HAPPY_STATE_THINKING);
        // Сервер перестал говорить — доигрываем остаток и чистим буфер,
        // иначе следующая реплика начнётся с хвоста предыдущей.
        if (s_state == HAPPY_STATE_IDLE) {
            happy_audio_out_flush();
        }
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
        happy_audio_out_push(data + 1, len - 1);
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
        send_hello();
        // Сразу слушаем: сервер ищет активационное слово в потоке, и ждать
        // первого сообщения о состоянии значит проглотить обращение.
        happy_audio_in_set_recording(true);
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
    return s_connected;
}

happy_state_t happy_ws_state(void)
{
    return s_state;
}

esp_err_t happy_ws_send_mic(const uint8_t *payload, size_t len)
{
    if (!s_connected) {
        return ESP_ERR_INVALID_STATE;
    }
    // Заголовок и данные должны уйти одним фреймом, поэтому склеиваем.
    static uint8_t frame[1 + HAPPY_MIC_FRAME_SAMPLES * 2];
    if (len + 1 > sizeof(frame)) {
        return ESP_ERR_INVALID_SIZE;
    }
    frame[0] = HAPPY_FRAME_MIC;
    memcpy(frame + 1, payload, len);
    int sent = esp_websocket_client_send_bin(s_client, (const char *)frame, len + 1,
                                             pdMS_TO_TICKS(100));
    return sent > 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t happy_ws_send_json(const char *json)
{
    if (!s_connected) {
        return ESP_ERR_INVALID_STATE;
    }
    int sent = esp_websocket_client_send_text(s_client, json, strlen(json), pdMS_TO_TICKS(200));
    return sent > 0 ? ESP_OK : ESP_FAIL;
}
