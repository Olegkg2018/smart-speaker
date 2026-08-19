#!/bin/sh
# Голос Piper — единственное, что нельзя скачать на старте автоматически:
# tts.load() падает с ошибкой, если файла нет. Догружаем его здесь, один раз
# на том, а не при каждом запуске контейнера.
set -e

VOICE="${PIPER_VOICE:-ru_RU-irina-medium}"

if [ ! -f "/app/models/${VOICE}.onnx" ]; then
    echo "качаю голос Piper ${VOICE} (~60 МБ, только при первом запуске)…"
    python -m piper.download_voices "$VOICE" --data-dir /app/models
fi

exec "$@"
