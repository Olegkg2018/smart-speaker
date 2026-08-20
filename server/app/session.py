"""Сессия одной колонки: конечный автомат поверх WebSocket-соединения."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

from app.audio.codec import Codec, make_codec
from app.audio.mixer import AudioMixer
from app.audio.vad import SilenceDetector
from app.config import Settings
from app.memory import ConversationMemory
from app.tools import alarms as alarms_tool
from app.protocol import (
    FRAME_MIC,
    FRAME_SCREEN,
    FRAME_SPEAKER,
    State,
    pack_audio,
    state_msg,
    unpack_audio,
    volume_msg,
)
from app.realtime import RealtimeVoice
from app.screen import ScreenRenderer
from app.stt import SpeechToText
from app.tools.context import ToolContext
from app.tts import TextToSpeech
from app.voice import ClaudeVoice, VoiceBackend, VoiceCallbacks
from app.wakeword import WakeWordModel

log = logging.getLogger(__name__)

# Дольше этого одна реплика не бывает — защита от залипшей кнопки.
_MAX_UTTERANCE_S = 30

# Короче этого реплика не бывает: обрывок шума после ложного пробуждения
# модель истолкует как продолжение прошлого разговора.
_MIN_UTTERANCE_MS = 600

# Кадр звука уходит каждые 20 мс. Опоздание больше половины кадра означает,
# что колонка досрочно доиграла буфер и слушатель услышал разрыв.
_FRAME_LATE_S = 0.010
_LATENCY_REPORT_S = 5.0


class Session:
    def __init__(
        self,
        ws: WebSocket,
        settings: Settings,
        stt: SpeechToText,
        tts: TextToSpeech,
        screen: ScreenRenderer | None = None,
        wake_model: WakeWordModel | None = None,
    ):
        self._ws = ws
        self._settings = settings
        # Синтез для серверных объявлений (сработавший таймер) — не зависит
        # от того, каким бэкендом ведётся сам разговор.
        self._tts = tts
        self._screen = screen
        self._screen_text = ""
        self._client_has_screen = False
        self._mixer = AudioMixer(
            frame_samples=settings.frame_samples_out,
            frame_ms=settings.frame_ms,
            duck_level=settings.duck_level,
            volume=settings.default_volume,
        )
        self._ctx = ToolContext(settings=settings, mixer=self._mixer, speak=self._announce)
        self._mixer.on_track_finished = self._play_next_in_queue
        callbacks = VoiceCallbacks(
            set_state=self._set_state,
            show_text=self._show,
            push_audio=self._mixer.push_speech,
            drop_audio=self._mixer.drop_speech,
            wait_drained=self._wait_speech_drained,
            turn_done=self._set_idle,
            save_turn=self._save_turn,
        )
        self._memory: ConversationMemory | None = None
        self._voice: VoiceBackend
        if settings.voice_provider == "openai_realtime":
            self._voice = RealtimeVoice(settings, self._ctx, callbacks)
        else:
            self._voice = ClaudeVoice(settings, self._ctx, stt, tts, callbacks)
        self._mic_codec: Codec = make_codec("pcm", settings.mic_sample_rate, 0)
        self._spk_codec: Codec = make_codec("pcm", settings.out_sample_rate, 0)
        self._vad = SilenceDetector(
            sample_rate=settings.mic_sample_rate,
            threshold=settings.vad_threshold,
            silence_ms=settings.vad_silence_ms,
            min_speech_ms=settings.vad_min_speech_ms,
            speech_factor=settings.vad_speech_factor,
        )
        # Модель общая на весь сервер (весит под сотню мегабайт), а
        # распознаватель — свой: KaldiRecognizer хранит состояние и портится,
        # когда в него пишут две сессии сразу.
        if wake_model is None:
            wake_model = WakeWordModel(
                settings.vosk_model_dir, settings.wake_word, settings.mic_sample_rate, []
            )
        self._wake = wake_model.for_session()
        self._recording = False
        self._mic_bytes = 0
        self._listen_started = 0.0
        self._state = State.IDLE
        self._device = "unknown"
        self._voice_failed = False
        self._alarm_tasks: list[asyncio.Task] = []

    # ---------- жизненный цикл ----------

    async def run(self) -> None:
        sender = asyncio.create_task(self._send_audio_loop())
        try:
            # Голосовой бэкенд стартует в _on_hello: только там известно имя
            # колонки, а значит — какую сохранённую память ему подсаживать.
            await self._receive_loop()
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            await self._shutdown()

    async def _shutdown(self) -> None:
        for task in self._alarm_tasks:
            task.cancel()
        self._alarm_tasks.clear()
        await self._voice.close()
        await self._ctx.cancel_timers()
        await self._mixer.set_music(None)
        log.info("сессия %s закрыта", self._device)

    # ---------- приём ----------

    async def _receive_loop(self) -> None:
        while True:
            packet = await self._ws.receive()
            if packet["type"] == "websocket.disconnect":
                return
            if (data := packet.get("bytes")) is not None:
                await self._on_binary(data)
            elif (text := packet.get("text")) is not None:
                await self._on_text(text)

    async def _on_binary(self, frame: bytes) -> None:
        kind, payload = unpack_audio(frame)
        if kind != FRAME_MIC:
            return
        pcm = self._mic_codec.decode(payload)

        if not self._recording:
            # Заодно замеряем, насколько тихо в комнате: к началу реплики
            # порог речи уже подстроен под обстановку.
            self._vad.observe_noise(pcm)

            # Пока играет музыка, микрофон слышит её же — эхоподавления на
            # плате нет. Распознаватель ловит слова из песни и колонка
            # выполняет команды, которых никто не давал. Поэтому во время
            # музыки активация только кнопкой.
            if self._mixer.is_playing and not self._settings.wake_word_while_playing:
                return

            # Ключевое место всей затеи: пока слово не прозвучало, звук
            # дальше этой строки не идёт — ни в облако, ни в бэкенд.
            if self._wake.available and await self._wake.feed(pcm):
                log.info("услышал «%s»", self._settings.wake_word)
                await self._start_recording()
            return

        self._mic_bytes += len(pcm)
        await self._voice.feed(pcm)
        if self._vad.feed(pcm):
            await self._stop_recording()
            return

        # Слово прозвучало, а команды не последовало — не держим микрофон
        # открытым: детектор тишины ждёт речи, которой не было, и сам
        # реплику никогда не закроет.
        if not self._vad.heard_speech:
            if time.monotonic() - self._listen_started > self._settings.wake_listen_timeout_s:
                log.info("после активации ничего не сказали — снова жду слово")
                await self._cancel_recording()
                return

        limit = self._settings.mic_sample_rate * _MAX_UTTERANCE_S * 2
        if self._mic_bytes >= limit:
            log.warning("реплика длиннее %d с — обрываю запись", _MAX_UTTERANCE_S)
            await self._stop_recording()

    async def _on_text(self, raw: str) -> None:
        try:
            msg: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("не разобрал текстовый фрейм: %.80s", raw)
            return

        match msg.get("t"):
            case "hello":
                await self._on_hello(msg)
            case "ptt":
                # Тап вместо удержания: одно и то же сообщение либо
                # начинает слушать, либо (если уже слушаем) досрочно
                # останавливает запись вручную. "up" от старой прошивки,
                # если вдруг придёт, просто игнорируем — конец реплики
                # теперь определяет детектор тишины, а не отпускание.
                if msg.get("state") == "down":
                    if self._recording:
                        await self._stop_recording()
                    else:
                        await self._start_recording()
            case "volume":
                await self._set_volume(float(msg.get("value", 0.7)))
            case "volume_step":
                # Колонка не знает текущую громкость — она живёт в микшере,
                # поэтому кнопки просят сдвиг, а не абсолютное значение.
                await self._set_volume(self._mixer.volume + float(msg.get("value", 0.0)))
            case unknown:
                log.warning("неизвестное сообщение: %s", unknown)

    async def _on_hello(self, msg: dict[str, Any]) -> None:
        self._device = msg.get("device", "unknown")
        self._client_has_screen = bool(msg.get("screen", False))
        codec_name = msg.get("codec", "pcm")
        self._mic_codec = make_codec(
            codec_name, self._settings.mic_sample_rate, self._settings.frame_samples_mic
        )
        self._spk_codec = make_codec(
            codec_name, self._settings.out_sample_rate, self._settings.frame_samples_out
        )
        log.info(
            "колонка «%s» подключилась, кодек %s (запрошен %s)",
            self._device,
            self._spk_codec.name,
            codec_name,
        )
        # Кодек мог не завестись — сообщаем клиенту фактический выбор.
        await self._ws.send_json({"t": "ready", "codec": self._spk_codec.name})
        await self._ws.send_json(volume_msg(self._mixer.volume))
        self._state = State.LISTENING  # чтобы следующий вызов точно перерисовал
        await self._set_state(State.IDLE)

        self._memory = ConversationMemory(
            self._settings.memory_dir, self._device, self._settings.memory_turns
        )
        try:
            await self._voice.start(self._memory.turns)
        except Exception:
            # Голосовой бэкенд не поднялся (неверный ключ, нет сети, квота).
            # Ронять из-за этого сессию нельзя: колонка уйдёт в бесконечный
            # цикл переподключения и даже не сможет сказать, что случилось.
            log.exception("голосовой бэкенд не запустился")
            self._voice_failed = True
            await self._announce("Не могу подключиться к голосовому сервису.")

        # Будильники поднимаем в любом случае: они не зависят от того,
        # работает ли разговор — разбудить нужно даже при сбое облака.
        self._restore_alarms()

    async def _set_volume(self, level: float) -> None:
        self._mixer.volume = max(0.0, min(1.0, level))
        with contextlib.suppress(Exception):
            await self._ws.send_json(volume_msg(self._mixer.volume))
        await self._show(f"Громкость {round(self._mixer.volume * 100)}%")

    def _restore_alarms(self) -> None:
        """Поднимает будильники с диска — их ставили в прошлой сессии.

        Ради этого они и лежат на диске: связь с колонкой рвётся регулярно,
        а будильник на утро должен пережить ночь целиком.
        """
        overdue, upcoming = alarms_tool.due_and_upcoming(self._settings.alarms_dir)
        for alarm in overdue:
            # Разбудить задним числом нельзя, а держать вечно — значит
            # звонить при каждом переподключении.
            log.info("будильник на %s пропущен, убираю", alarm.at)
            alarms_tool.drop(self._settings.alarms_dir, alarm.id)

        for alarm in upcoming:
            task = asyncio.create_task(alarms_tool.wait_and_fire(alarm, self._fire_alarm))
            self._alarm_tasks.append(task)
        if upcoming:
            log.info("восстановлено будильников: %d", len(upcoming))

    async def _fire_alarm(self, alarm) -> None:
        """Будит: голосом, а если просили — ещё и звуком."""
        alarms_tool.drop(self._settings.alarms_dir, alarm.id)
        text = f"Пора вставать. {alarm.label}." if alarm.label else "Пора вставать."

        if alarm.sound:
            from app.tools import music

            # Музыку включаем первой: она заиграет фоном, а голос прозвучит
            # поверх — иначе человек услышит фразу в тишине и снова уснёт.
            try:
                await music.play_music(self._ctx, alarm.sound)
            except Exception:
                log.exception("не удалось включить звук будильника")
        await self._announce(text)

    async def _play_next_in_queue(self) -> None:
        """Трек доиграл — включаем следующий из плейлиста.

        Молча: объявлять каждую песню голосом посреди музыки утомительно.
        """
        nxt = self._ctx.next_in_queue()
        if nxt is None:
            self._ctx.queue_name = None
            return
        from app.tools import music

        log.info("плейлист «%s»: следующий трек %s", self._ctx.queue_name, nxt)
        try:
            await music.play_music(self._ctx, nxt)
        except Exception:
            log.exception("не удалось включить следующий трек плейлиста")

    async def _save_turn(self, role: str, text: str) -> None:
        if self._memory is not None:
            self._memory.append(role, text)

    # ---------- запись и обработка ----------

    async def _start_recording(self) -> None:
        if self._voice_failed:
            # Слушать некому — честно говорим об этом, а не молчим в ответ.
            await self._announce("Голосовой сервис недоступен.")
            return
        await self._voice.begin_utterance()
        self._mic_bytes = 0
        self._vad.reset()
        self._listen_started = time.monotonic()
        self._recording = True
        await self._set_state(State.LISTENING)

    async def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False

        # Пустую реплику отправлять нельзя. Модель получает шум, не находит
        # в нём команды — и отвечает по прошлому контексту: «включал музыку,
        # хотя я не просил». Лучше молча вернуться к ожиданию.
        speech_bytes = self._settings.mic_sample_rate * _MIN_UTTERANCE_MS // 1000 * 2
        if not self._vad.heard_speech or self._mic_bytes < speech_bytes:
            log.info("реплика пустая или слишком короткая — не отправляю")
            self._wake.reset()
            await self._voice.barge_in()
            await self._set_idle()
            return

        await self._voice.end_utterance()

    async def _cancel_recording(self) -> None:
        """Закрыть микрофон, не отправляя записанное: сказать было нечего."""
        if not self._recording:
            return
        self._recording = False
        self._wake.reset()
        await self._voice.barge_in()
        await self._set_idle()

    async def _announce(self, text: str) -> None:
        """Реплика по инициативе сервера — например, сработавший таймер.

        Всегда идёт через локальный Piper, а не через голосовой бэкенд
        разговора: это разовая фраза вне контекста диалога.
        """
        await self._set_state(State.SPEAKING)
        await self._show(text)
        pcm = await self._tts.synthesize(text)
        if pcm:
            await self._mixer.push_speech(pcm)
        await self._wait_speech_drained()
        await self._set_idle()

    async def _wait_speech_drained(self) -> None:
        while self._mixer.is_speaking:
            await asyncio.sleep(0.05)

    # ---------- отправка ----------

    async def _send_audio_loop(self) -> None:
        """Ровно один кадр за `frame_ms`, без накопления дрейфа.

        Здесь же меряется здоровье звукового тракта. Кадр обязан уходить
        каждые двадцать миллисекунд; если сервер занят и опаздывает, колонка
        получает звук рывками — на слух это «прерывистый разговор». Опоздания
        копятся и раз в несколько секунд попадают в лог, чтобы догадки
        «наверное, железо не тянет» можно было заменить числами.
        """
        period = self._settings.frame_ms / 1000
        next_tick = time.monotonic()
        late_frames = 0
        total_frames = 0
        worst_late = 0.0
        next_report = time.monotonic() + _LATENCY_REPORT_S

        while True:
            next_tick += period
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

            # Пока идёт реплика или играет музыка, поток обязан быть
            # непрерывным, даже если в микшере сейчас пусто. Иначе колонка
            # доигрывает буфер, объявляет поток кончившимся и уходит на
            # повторный прогрев — а это стапятьдесят миллисекунд тишины
            # посреди фразы. Облачная модель отдаёт звук порциями, и такие
            # просветы между ними — обычное дело.
            speaking = self._state in (State.SPEAKING, State.THINKING)
            if not (speaking or self._mixer.is_speaking or self._mixer.is_playing):
                # В покое тишину не шлём: на PCM это полтора мегабита в никуда.
                # Заодно сбрасываем отсчёт, иначе пауза в разговоре
                # засчиталась бы как гигантское опоздание.
                next_tick = time.monotonic()
                continue

            late = time.monotonic() - next_tick
            total_frames += 1
            if late > _FRAME_LATE_S:
                late_frames += 1
                worst_late = max(worst_late, late)

            pcm = await self._mixer.next_frame()
            packet = self._spk_codec.encode(pcm)
            await self._ws.send_bytes(pack_audio(FRAME_SPEAKER, packet))

            now = time.monotonic()
            if now >= next_report:
                next_report = now + _LATENCY_REPORT_S
                if late_frames:
                    log.warning(
                        "звук идёт рывками: %d из %d кадров опоздали больше %.0f мс "
                        "(худшее опоздание %.0f мс)",
                        late_frames, total_frames, _FRAME_LATE_S * 1000, worst_late * 1000,
                    )
                elif total_frames:
                    log.info("звук ровный: %d кадров без опозданий", total_frames)
                late_frames = total_frames = 0
                worst_late = 0.0

    # ---------- состояние ----------

    async def _set_idle(self) -> None:
        await self._set_state(State.PLAYING if self._mixer.is_playing else State.IDLE)

    async def _set_state(self, state: State) -> None:
        if state == self._state:
            return
        self._state = state
        if state in (State.LISTENING, State.IDLE):
            # Новый заход — старая реплика на экране только путает.
            self._screen_text = ""
        with contextlib.suppress(Exception):
            await self._ws.send_json(state_msg(state))
        await self._redraw()

    # ---------- экран ----------

    async def _show(self, text: str) -> None:
        self._screen_text = text
        await self._redraw()

    async def _redraw(self) -> None:
        if self._screen is None or not self._screen.available or not self._client_has_screen:
            return
        bitmap = self._screen.render(self._state, self._screen_text, self._ctx.now_playing)
        if not bitmap:
            return
        with contextlib.suppress(Exception):
            await self._ws.send_bytes(pack_audio(FRAME_SCREEN, bitmap))
