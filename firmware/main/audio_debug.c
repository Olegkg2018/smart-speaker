#include "app.h"

#include <string.h>

#include "esp_log.h"

#if CONFIG_HAPPY_AUDIO_DEBUG

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

static const char *TAG = "audio_debug";

// Отправляем то, что слышит микрофон, по UDP — чтобы это можно было
// послушать на компьютере. Приём тот же, что в audio/audio_debugger.cc
// у 78/xiaozhi-esp32.
//
// Зачем: качество распознавания мы до сих пор оценивали по косвенным
// признакам — расшифровкам на чужих языках, доле опоздавших кадров,
// логам. Гадать, слышит ли колонка эхо собственного динамика или
// человека, бессмысленно, когда можно просто послушать.
//
// Поток сырой: PCM 16 бит, моно, частота микрофона. Заголовков нет —
// на той стороне достаточно записать байты в файл и открыть их как raw.

static int s_sock = -1;
static struct sockaddr_in s_dest;

esp_err_t happy_audio_debug_start(void)
{
    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s_sock < 0) {
        ESP_LOGW(TAG, "не открылся UDP-сокет, отладка звука выключена");
        return ESP_OK;  // не смертельно: колонка обязана работать и без этого
    }

    memset(&s_dest, 0, sizeof(s_dest));
    s_dest.sin_family = AF_INET;
    s_dest.sin_port = htons(CONFIG_HAPPY_AUDIO_DEBUG_PORT);
    s_dest.sin_addr.s_addr = inet_addr(CONFIG_HAPPY_AUDIO_DEBUG_HOST);

    ESP_LOGI(TAG, "звук с микрофона дублируется на %s:%d",
             CONFIG_HAPPY_AUDIO_DEBUG_HOST, CONFIG_HAPPY_AUDIO_DEBUG_PORT);
    return ESP_OK;
}

void happy_audio_debug_feed(const int16_t *pcm, size_t samples)
{
    if (s_sock < 0 || pcm == NULL || samples == 0) {
        return;
    }
    // Не ждём сеть: этот вызов сидит на пути кадра микрофона, и задержка
    // здесь превратилась бы в опоздание звука. Не ушло — и ладно, это
    // диагностика, а не данные.
    sendto(s_sock, pcm, samples * sizeof(int16_t), MSG_DONTWAIT,
           (struct sockaddr *)&s_dest, sizeof(s_dest));
}

#else  // отладка звука выключена в menuconfig

esp_err_t happy_audio_debug_start(void) { return ESP_OK; }
void happy_audio_debug_feed(const int16_t *pcm, size_t samples)
{
    (void)pcm;
    (void)samples;
}

#endif
