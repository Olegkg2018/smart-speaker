# syntax=docker/dockerfile:1

# Собирается прямо на S905X3 — все нужные колёса (ctranslate2, piper-tts)
# есть под aarch64 готовыми, компиляции из исходников не происходит.
FROM python:3.12-slim

# ffmpeg декодирует музыку в PCM для микшера; DejaVu даёт кириллицу на экране
# колонки — картинку рисует сервер, поэтому шрифт нужен именно здесь.
# libopus0 — рантайм-библиотека для opuslib (ctypes-обёртка, не компилирует
# ничего сама, но без .so падает импортом): без неё make_codec("opus", ...)
# молча откатывается на PCM, а колонка так и не узнаёт о экономии трафика.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        ca-certificates \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Активационное слово для сателлита (app/static/wakeword.js) — модели
# openWakeWord отдаёт наш сервер, телефон считает их у себя. В образ, а не
# в том: это часть кода страницы, и версия должна ехать вместе с ним.
# Раньше COPY app — чтобы правка кода не качала модели заново. Не в /app:
# setuptools принимает любую папку там за второй пакет и роняет pip install.
ARG OWW_URL=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
RUN mkdir -p /opt/wakeword && cd /opt/wakeword \
    && for f in melspectrogram.onnx embedding_model.onnx hey_jarvis_v0.1.onnx; do \
         python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
           "$OWW_URL/$f" "$f"; done \
    && echo "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f  melspectrogram.onnx" > SHA256SUMS \
    && echo "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f  embedding_model.onnx" >> SHA256SUMS \
    && echo "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb  hey_jarvis_v0.1.onnx" >> SHA256SUMS \
    && sha256sum -c SHA256SUMS

COPY pyproject.toml ./
COPY app ./app

# Кэш pip живёт между сборками: на четырёх Cortex-A55 повторная выкачка
# трёхсот мегабайт колёс стоит несколько минут.
RUN --mount=type=cache,target=/root/.cache/pip pip install --editable .[opus]

# Замер скорости на живом железе — единственное, ради чего сюда попадает
# каталог tools; на пересборку кода он не влияет.
COPY tools ./tools

COPY docker/entrypoint.sh /usr/local/bin/happy-entrypoint
RUN chmod +x /usr/local/bin/happy-entrypoint

# Модели и музыка приезжают томами; каталоги создаём заранее и отдаём
# непривилегированному пользователю — том наследует владельца от образа.
RUN mkdir -p /app/models /app/music /app/data/memory \
    && useradd --uid 1000 --create-home happy \
    && chown -R happy:happy /app
USER happy

# Веса whisper тянутся с Hugging Face при первом старте — пусть ложатся в том,
# иначе каждый пересоздаваемый контейнер качает их заново.
ENV HF_HOME=/app/models/hf \
    WAKEWORD_DIR=/opt/wakeword \
    PYTHONUNBUFFERED=1

EXPOSE 8080
ENTRYPOINT ["happy-entrypoint"]
CMD ["python", "-m", "app.main"]
