#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

// --- параметры аудио, должны совпадать с server/app/config.py ---
#define HAPPY_MIC_SAMPLE_RATE 16000
#define HAPPY_SPK_SAMPLE_RATE 48000
#define HAPPY_FRAME_MS 20
#define HAPPY_MIC_FRAME_SAMPLES (HAPPY_MIC_SAMPLE_RATE / 1000 * HAPPY_FRAME_MS)  // 320
#define HAPPY_SPK_FRAME_SAMPLES (HAPPY_SPK_SAMPLE_RATE / 1000 * HAPPY_FRAME_MS)  // 960

// --- экран ---
#define HAPPY_SCREEN_WIDTH 128
#define HAPPY_SCREEN_HEIGHT 64

// --- типы бинарных фреймов (см. server/app/protocol.py) ---
#define HAPPY_FRAME_MIC 0x01u
#define HAPPY_FRAME_SPEAKER 0x02u
#define HAPPY_FRAME_SCREEN 0x03u

typedef enum {
    HAPPY_STATE_IDLE = 0,
    HAPPY_STATE_LISTENING,
    HAPPY_STATE_THINKING,
    HAPPY_STATE_SPEAKING,
    HAPPY_STATE_PLAYING,
} happy_state_t;

// --- Wi-Fi ---
esp_err_t happy_wifi_start(void);
void happy_wifi_wait_connected(void);

// --- WebSocket ---
esp_err_t happy_ws_start(void);
bool happy_ws_connected(void);
esp_err_t happy_ws_send_mic(const uint8_t *payload, size_t len);
esp_err_t happy_ws_send_json(const char *json);
happy_state_t happy_ws_state(void);

// --- аудио ---
esp_err_t happy_audio_in_start(void);
void happy_audio_in_set_recording(bool recording);
esp_err_t happy_audio_out_start(void);
// Вызывается из обработчика WebSocket: кладёт полученный кадр в буфер вывода.
void happy_audio_out_push(const uint8_t *pcm, size_t len);
void happy_audio_out_flush(void);

// --- экран ---
// Картинку целиком рисует сервер; прошивка только выводит готовый битмап.
esp_err_t happy_display_start(void);
void happy_display_draw(const uint8_t *pages, size_t len);
void happy_display_clear(void);

// --- светодиод ---
// На плате стоит адресный WS2812, а не обычный: состояние показываем цветом.
esp_err_t happy_led_start(void);

// --- кнопки ---
typedef enum {
    HAPPY_BUTTON_TALK = 0,
    HAPPY_BUTTON_VOL_DOWN,
    HAPPY_BUTTON_VOL_UP,
} happy_button_t;

typedef void (*happy_button_cb_t)(happy_button_t button, bool pressed);
esp_err_t happy_button_start(happy_button_cb_t on_change);
