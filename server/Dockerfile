# syntax=docker/dockerfile:1

# Собирается прямо на S905X3 — все нужные колёса (ctranslate2, piper-tts)
# есть под aarch64 готовыми, компиляции из исходников не происходит.
FROM python:3.12-slim

# ffmpeg декодирует музыку в PCM для микшера; DejaVu даёт кириллицу на экране
# колонки — картинку рисует сервер, поэтому шрифт нужен именно здесь.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

# Кэш pip живёт между сборками: на четырёх Cortex-A55 повторная выкачка
# трёхсот мегабайт колёс стоит несколько минут.
RUN --mount=type=cache,target=/root/.cache/pip pip install --editable .

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
    PYTHONUNBUFFERED=1

EXPOSE 8080
ENTRYPOINT ["happy-entrypoint"]
CMD ["python", "-m", "app.main"]
