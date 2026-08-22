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
// Эхоподавитель отдаёт звук кусками своей длины, а не микрофонными кадрами:
// на ESP32-S3 это 512 сэмплов. Буфер отправки считаем по этому размеру.
#define HAPPY_MIC_MAX_FRAME_SAMPLES 512
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

// --- Opus ---
// Экономит трафик в ~15 раз против сырого PCM (см. server/app/audio/codec.py).
// Кодек согласуется в hello/ready — если сервер не поддерживает Opus
// (например, не установлен libopus), обе стороны честно остаются на PCM.
// Само кодирование/декодирование живёт в своей задаче с низким приоритетом
// (см. audio_opus.c) — синхронные вызовы из afe_fetch/ws-клиента роняли их
// собственные тайминги. submit-функции только кладут данные в очередь и
// сразу возвращаются; не ждут, что кадр обработается прямо сейчас.
esp_err_t happy_opus_init(void);
bool happy_opus_available(void);
// Сбрасывает внутреннее состояние кодировщика/декодировщика и накопленный
// остаток кадра — перед каждым новым соединением, чтобы не тащить огрызок
// от прошлой сессии.
void happy_opus_reset(void);
// Неблокирующая постановка сырых сэмплов микрофона в очередь на кодирование.
void happy_opus_submit_encode(const int16_t *pcm, size_t samples);
// Неблокирующая постановка принятого пакета в очередь на декодирование.
void happy_opus_submit_decode(const uint8_t *packet, size_t packet_len);

// --- WebSocket ---
esp_err_t happy_ws_start(void);
bool happy_ws_connected(void);
// Поднять соединение заново, когда клиент застрял: он умеет молча
// перестать переподключаться, продолжая ругаться в лог.
void happy_ws_restart(void);
esp_err_t happy_ws_send_mic(const uint8_t *payload, size_t len);
// Отправка уже готового кадра (сырой PCM или закодированный Opus-пакет) —
// задача кодека зовёт это напрямую, когда закончит кодирование.
esp_err_t happy_ws_send_mic_raw(const uint8_t *payload, size_t len);
esp_err_t happy_ws_send_json(const char *json);
happy_state_t happy_ws_state(void);

// --- аудио ---
esp_err_t happy_audio_in_start(void);
void happy_audio_in_set_recording(bool recording);
// Микрофон слушает всегда — ради активационного слова. Этот флаг говорит
// лишь о том, уходит ли звук на сервер.
bool happy_audio_in_is_recording(void);
esp_err_t happy_audio_out_start(void);
// Вызывается из обработчика WebSocket: кладёт полученный кадр в буфер вывода.
void happy_audio_out_push(const uint8_t *pcm, size_t len);
void happy_audio_out_flush(void);
// Копия того, что уходит в динамик, приведённая к частоте микрофона.
// Эхоподавителю нужен опорный сигнал: он вычитает из микрофона то, что
// колонка играет сама, иначе она слышит собственную музыку и выполняет
// команды из песни. Возвращает, сколько сэмплов удалось отдать; недостачу
// вызывающий дополняет тишиной.
size_t happy_audio_out_take_reference(int16_t *dst, size_t samples);

// --- обработка звука с микрофона ---
// Эхоподавление (микрофон перестаёт слышать собственный динамик) плюс
// автоусиление: без него слышно только вплотную. Если поднять не удалось,
// звук идёт дальше необработанным — колонка продолжает работать.
typedef void (*happy_frontend_cb_t)(const int16_t *pcm, size_t samples);
// Активационное слово услышано — колонка сама начинает разговор.
typedef void (*happy_wake_cb_t)(void);
esp_err_t happy_frontend_start(happy_frontend_cb_t on_clean, happy_wake_cb_t on_wake);
void happy_frontend_stop(void);
bool happy_frontend_available(void);
// Пока колонка говорит или играет музыку, слово лучше не слушать: даже с
// эхоподавлением остаётся риск услышать себя.
void happy_frontend_set_wake_enabled(bool enabled);
// Кадры на входе и выходе разной длины: обработчик копит их до своего
// размера, а результат отдаёт колбэком из своей задачи.
void happy_frontend_process(const int16_t *mic, size_t samples);

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
