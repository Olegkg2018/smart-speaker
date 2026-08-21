"""Конфигурация сервера. Значения берутся из .env или переменных окружения."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVER_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- сеть ---
    host: str = "0.0.0.0"
    port: int = 8080

    # --- аудио ---
    # Микрофон колонки: 16 кГц достаточно для распознавания речи.
    mic_sample_rate: int = 16_000
    # Выход: 48 кГц — родная частота Opus и большинства музыкальных источников.
    out_sample_rate: int = 48_000
    frame_ms: int = 20
    # До какой доли громкости приглушать музыку, пока говорит ассистент.
    duck_level: float = 0.2
    default_volume: float = 0.7

    # --- активация прослушивания ---
    # Кнопка только начинает слушать (тап, не удержание) — конец реплики
    # определяет этот детектор тишины по амплитуде, а не отпускание кнопки.
    # Нижняя граница громкости речи (амплитуда PCM s16le). Порог адаптивный:
    # реальный считается от фонового шума комнаты, а это значение — пол, ниже
    # которого речью не считаем ничего. С фиксированным порогом колонка слышала
    # только вплотную: голос с двух метров тише и уходил в «тишину».
    vad_threshold: int = 150
    # Во сколько раз речь должна быть громче фона.
    vad_speech_factor: float = 3.0
    # Столько подряд тишины после речи считается концом реплики.
    vad_silence_ms: int = 900
    # Тишина короче этого времени с начала записи концом не считается —
    # иначе вдох перед фразой сразу же её оборвёт.
    vad_min_speech_ms: int = 200

    # --- активационное слово ---
    # Пока оно не прозвучало, звук не уходит в облако и денег не стоит.
    # Слово ищется локально, моделью Vosk. Кнопка продолжает работать.
    wake_word_enabled: bool = False
    # Фраза из двух слов надёжнее одного: одиночное слово ловится на
    # случайных созвучиях в речи и в песнях, два подряд — почти никогда.
    wake_word: str = "слушай компьютер"
    # Микрофон слышит музыку, которую играет сама колонка, а эхоподавления
    # на плате нет — распознаватель ловит слова из песни и выполняет команды,
    # которых никто не давал. Пока играет музыка, активация только кнопкой.
    wake_word_while_playing: bool = False
    # Замерено: короткие слова притягивают созвучия («Алиса» ловится на
    # «Ларису» и «Мелиссу»), длинные — нет. Если всё же нужно короткое,
    # перечислите похожие слова здесь: распознавателю будет куда их деть.
    wake_word_neighbours: list[str] = []
    vosk_model_dir: Path = SERVER_ROOT / "models" / "vosk-ru"
    # Сколько слушать после активации, если человек молчит.
    wake_listen_timeout_s: float = 8.0

    # --- STT ---
    # Замерено на S905X3 (tools/bench.py): `base` — 4.6–5.0 с на фразу,
    # `tiny` — 2.6 с, но заметно хуже слышит. Время почти не зависит от длины
    # реплики: Whisper всегда считает окно в 30 с, и на этом процессоре сам
    # энкодер стоит несколько секунд. Пауза после кнопки неизбежна.
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = "ru"
    whisper_cpu_threads: int = 4

    # --- TTS ---
    piper_voice: str = "ru_RU-irina-medium"
    models_dir: Path = SERVER_ROOT / "models"

    # --- экран колонки ---
    # Картинку для OLED рисует сервер и шлёт готовым битмапом: так на экране
    # есть полноценная кириллица без возни со шрифтами в прошивке.
    screen_enabled: bool = True
    screen_width: int = 128
    screen_height: int = 64
    screen_font: Path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

    # --- LLM ---
    anthropic_api_key: str = ""
    # Точный ID без суффикса даты. Менять только осознанно.
    model: str = "claude-opus-5"
    # Голосовой диалог чувствителен к задержке, поэтому низкий effort.
    # Мышление при этом остаётся включённым (на Opus 5 это надёжнее, чем
    # thinking=disabled, который умеет писать вызов инструмента текстом).
    effort: str = "low"
    max_tokens: int = 4096
    max_history_turns: int = 20

    # --- память между переподключениями ---
    # Текст разговора (без аудио) переживает обрыв связи и перезапуск сервера —
    # хранится в memory_dir/<имя колонки>.json, без шифрования (домашний сервер).
    memory_dir: Path = SERVER_ROOT / "data" / "memory"
    # Списки покупок и дел: обычные файлы рядом с памятью, их можно открыть
    # и поправить руками, не заходя в контейнер.
    lists_dir: Path = SERVER_ROOT / "data" / "lists"
    # Будильники живут на диске, а не в сессии: связь с колонкой рвётся
    # регулярно, а разбудить утром надо независимо от этого.
    alarms_dir: Path = SERVER_ROOT / "data" / "alarms"
    # Постоянные заметки («запомни, что…»): в отличие от истории разговора
    # они не вытесняются новыми репликами и идут в инструкции каждый раз.
    notes_dir: Path = SERVER_ROOT / "data" / "notes"
    # Модель для поиска в интернете. Отдельный, второй вызов к OpenAI:
    # разговор идёт через Realtime, а в сеть ходит Responses API.
    web_search_model: str = "gpt-4o-mini"

    # --- дублирование разговора в Telegram ---
    # У колонки нет экрана и истории под рукой: пересылка в личные сообщения
    # даёт возможность посмотреть разговор с телефона и понять, что она
    # расслышала, когда ответ выглядит странно. Токен бота от @BotFather.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- умный дом ---
    # Сенсоры читаются напрямую (доли секунды, точное значение), а сложные
    # команды отдаются встроенному ассистенту HA: он уже знает все устройства
    # по именам, и держать этот справочник у себя значит расходиться с
    # реальностью после каждого нового выключателя.
    ha_url: str = ""
    ha_token: str = ""
    # Пока только чтение: ошибка распознавания не должна ничего включать.
    ha_allow_control: bool = False

    # --- диагностика ---
    # Ищет корутины, которые надолго занимают цикл событий: именно из-за них
    # кадры звука уходят с опозданием и речь идёт рывками.
    debug_slow_callbacks: bool = False
    debug_slow_callback_s: float = 0.05
    memory_turns: int = 20  # реплик пользователя + примерно столько же ответов

    # --- голосовой бэкенд ---
    # "claude": Whisper → Claude с инструментами → Piper (по умолчанию, локально).
    # "openai_realtime": спич-ту-спич без STT/TTS — платно, зато без паузы на
    # распознавание (см. tools/bench.py) и без роботизированного Piper.
    voice_provider: str = "claude"
    # Распознавание и синтез можно по отдельности вынести в облако OpenAI,
    # оставив мозги на Claude. На S905X3 это главный рычаг: локальный Whisper
    # стоит ~5 с на фразу, а Piper звучит роботом. Нужен OPENAI_API_KEY.
    # "local" — faster-whisper / Piper, "openai" — whisper-1 / tts-1.
    stt_provider: str = "local"
    tts_provider: str = "local"
    openai_stt_model: str = "whisper-1"
    openai_tts_model: str = "tts-1"
    # Голоса tts-1: alloy, echo, fable, onyx, nova, shimmer.
    openai_tts_voice: str = "nova"
    openai_api_key: str = ""
    openai_realtime_model: str = "gpt-realtime"
    openai_voice: str = "marin"
    # Realtime переотправляет весь контекст сессии на каждый ответ, поэтому
    # входной счёт растёт с каждой репликой: разговор оплачивается заново
    # целиком. Здесь ставится потолок — по его достижении старое отбрасывается.
    realtime_context_tokens: int = 4000
    # Какую долю оставлять при обрезке: 0.6 — выкинуть примерно сорок процентов
    # самого старого, чтобы не резать на каждой следующей реплике.
    realtime_retention_ratio: float = 0.6

    # --- инструменты ---
    default_city: str = "Кривий Ріг"
    default_latitude: float = 47.9105
    default_longitude: float = 33.3918
    # Несколько лент вместо одной: сайты падают и меняют адреса, а колонка
    # должна отвечать хоть что-то. Заголовки берутся по кругу, а не подряд
    # из первой ленты — иначе новости всегда с одного источника.
    news_feeds: list[str] = [
        "https://www.pravda.com.ua/rss/",
        "https://suspilne.media/rss/all.rss",
        "https://rss.unian.net/site/news_ukr.rss",
        "https://www.liga.net/news/all/rss.xml",
    ]
    # Публичные Telegram-каналы: читаются через веб-превью, без API и токенов.
    # Идут наравне с лентами СМИ — местные новости часто есть только здесь.
    news_telegram_channels: list[str] = ["insiderUKR", "hyevuy_k_r"]
    music_dir: Path = SERVER_ROOT / "music"
    # Разметка фонотеки по жанру/настроению/поводу — обычная папка на диске,
    # как остальное в data/, чтобы её можно было стереть руками, если модель
    # ошиблась. Строится и обновляется ночью в main.py, читается в player.py.
    music_index_dir: Path = SERVER_ROOT / "data" / "music_index"

    @property
    def frame_samples_out(self) -> int:
        """Сколько сэмплов в одном исходящем фрейме."""
        return self.out_sample_rate * self.frame_ms // 1000

    @property
    def frame_samples_mic(self) -> int:
        return self.mic_sample_rate * self.frame_ms // 1000


settings = Settings()
