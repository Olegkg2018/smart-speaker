#!/bin/sh
# Голос Piper — единственное, что нельзя скачать на старте автоматически:
# tts.load() падает с ошибкой, если файла нет. Догружаем его здесь, один раз
# на том, а не при каждом запуске контейнера.
set -e

VOICE="${PIPER_VOICE:-ru_RU-irina-medium}"

# Piper нужен только пути TTS_PROVIDER=local — с TTS_PROVIDER=openai его
# никто не вызывает, и качать 60 МБ, которые лягут мёртвым грузом, незачем.
if [ "${TTS_PROVIDER:-local}" = "local" ] && [ ! -f "/app/models/${VOICE}.onnx" ]; then
    echo "качаю голос Piper ${VOICE} (~60 МБ, только при первом запуске)…"
    python -m piper.download_voices "$VOICE" --data-dir /app/models
fi

exec "$@"
