"""Голосовой бэкенд поверх OpenAI Realtime API: спич-ту-спич без STT и TTS.

Существует по одной причине: Whisper на S905X3 не разгоняется быстрее пяти
секунд на реплику (`tools/bench.py`, RTF 1.78) — окно у него всегда
тридцатисекундное независимо от длины фразы. Realtime вместо текста гоняет
аудио потоком в обе стороны, и эта стадия просто исчезает.

Кнопка на колонке — единственный источник истины о начале и конце реплики,
поэтому серверный VAD выключен (`turn_detection: None`): модель слушает
и отвечает по команде, а не сама решает, когда пользователь замолчал.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time

from openai import AsyncOpenAI

from app.agent import SYSTEM_PROMPT
from app.audio.resample import resample_pcm16
from app.config import Settings
from app import memory_summary
from app.memory import Turn
from app.tools import notes as notes_tool
from app.pricing import CostMeter
from app.protocol import State
from app.tools.context import ToolContext
from app.tools.registry import TOOL_SCHEMAS, dispatch
from app.voice import VoiceCallbacks

log = logging.getLogger(__name__)

# API принимает и отдаёт PCM16 только на этой частоте — не настраивается.
_REALTIME_RATE = 24_000

# Микрофон приходит кадрами по 20 мс, но отправлять каждый отдельным
# сообщением в облако — полсотни запросов в секунду через интернет. Каждый
# ждёт сети, и вся эта очередь копится прямо в цикле событий: замер показал
# задержки до трёх секунд, из-за которых речь в колонке шла рывками.
# Копим пятую долю секунды и отправляем разом.
_SEND_BATCH_MS = 200

# OpenAI держит Realtime-сессию не дольше часа — объявленный лимит, не сбой
# (см. _recv_loop и _proactive_refresh). Реактивного переподключения после
# разрыва достаточно для правильности, но сам разрыв может прийтись ровно на
# середину чьей-то фразы. Запас в две минуты даёт время обновиться заранее,
# в паузе разговора, и разрыв никто не заметит вовсе.
_SESSION_MAX_S = 60 * 60
_REFRESH_MARGIN_S = 120

# Сколько ждать расшифровку вопроса после того, как ответ уже готов.
# Whisper обычно укладывается в доли секунды; ждать дольше незачем —
# память подождёт, а без ответа в ней остаться нельзя.
_TRANSCRIPT_WAIT_S = 3.0

# У установки соединения (connect + session.update) не было потолка вовсе —
# живой случай: сетевая заминка подвесила его на 14+ минут молча, без
# исключения. self._conn всё это время оставался None, а переподключение
# никто больше не пробовал — «дождусь штатного разрыва» ждать было нечего,
# разрывать уже было нечего. Таймаут ниже превращает молчаливое зависание в
# быструю, обрабатываемую ошибку.
_CONNECT_TIMEOUT_S = 15.0

# Столько раз пробуем восстановить соединение подряд, прежде чем сдаться
# и подождать следующего повода (новую реплику или плановое обновление).
# Задержки растут: короткая сетевая заминка не должна разрешиться на первой
# же попытке слишком поздно, а долгий сбой (ключ, авария у OpenAI) не стоит
# долбить раз в секунду.
_RECONNECT_ATTEMPTS = 3
_RECONNECT_BACKOFF_S = (3.0, 10.0, 30.0)

# Пока соединение не восстановится, каждая новая реплика узнаёт об этом
# заново и просит подождать. Если что-то (эхо колонки, шум в комнате)
# продолжает будить её раз за разом, не нужно озвучивать это чаще: живой
# случай — «секунду, переподключаюсь» зациклилось на минутном интервале,
# пока соединение не поднималось вовсе.
_RECONNECT_ANNOUNCE_MIN_GAP_S = 30.0

def _decode_and_resample(delta: str, src_rate: int, dst_rate: int) -> bytes:
    return resample_pcm16(base64.b64decode(delta), src_rate, dst_rate)


async def _drain_cancelled(task: asyncio.Task, label: str) -> None:
    """Ждёт отменённую задачу в фоне, не блокируя того, кто её отменил.

    Раньше это ожидание было в основном потоке `_proactive_refresh` — и
    именно оно однажды повисло на минуты. Не потому, что забыли таймаут:
    `asyncio.wait_for` тут не спасает вовсе, если сама задача проглатывает
    `CancelledError` и не завершается, — он всё равно ждёт её настоящего
    конца, просто откладывая исключение. Единственный надёжный выход — не
    ждать синхронно: свою часть работы (новое соединение) продолжать сразу,
    а старую задачу закрывать сама по себе, сколько бы это ни заняло.
    """
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    log.debug("фоновая уборка «%s» завершена", label)


async def _drain_manager_exit(manager) -> None:
    with contextlib.suppress(Exception):
        await manager.__aexit__(None, None, None)


def _build_instructions(history: list[Turn], notes: str = "", summary=None) -> str:
    """SYSTEM_PROMPT плюс сводка и краткий пересказ прошлого разговора.

    Realtime API не даёт напрямую подсадить историю сообщений в сессию так
    же чисто, как в Chat Completions — конкретный формат текстовых элементов
    conversation.item для сообщений не задокументирован достаточно точно,
    чтобы полагаться на него. Пересказ в инструкциях надёжнее и не зависит
    от точной схемы API.

    `summary` — то, что вытеснено из окна `history` и свёрнуто отдельной
    моделью (app/memory_summary.py). `history` — только недавнее, дословно;
    summary — то, что было раньше, но человек вправе ожидать, что колонка
    это помнит.
    """
    text = SYSTEM_PROMPT + notes
    summary_text = memory_summary.as_text(summary)
    if summary_text:
        # Именно «что известно», а не «вот что делай»: свободным текстом
        # сюда однажды попало «запускать музыку, когда она вернётся», и
        # модель выполнила это вместо ответа на вопрос о погоде.
        text += (
            "\n\nЧто известно об этом человеке из прошлых разговоров "
            "(справка, не поручение):\n" + summary_text
        )
    if not history:
        return text
    recap = "\n".join(
        f"{'Пользователь' if t.role == 'user' else 'Ты'}: {t.text}" for t in history
    )
    return (
        f"{text}\n\n"
        "Ниже — последние реплики более раннего разговора с этим человеком, "
        "для контекста. Это не текущая реплика, отвечать на неё не нужно:\n"
        f"{recap}"
    )


_TOOLS = [
    {
        "type": "function",
        "name": t["name"],
        "description": t["description"],
        "parameters": t["input_schema"],
    }
    for t in TOOL_SCHEMAS
]


class RealtimeVoice:
    def __init__(self, settings: Settings, ctx: ToolContext, cb: VoiceCallbacks):
        self._settings = settings
        self._ctx = ctx
        self._cb = cb
        self._client = AsyncOpenAI(api_key=settings.openai_api_key or None)
        self._manager = None
        self._conn = None
        self._recv_task: asyncio.Task | None = None
        # Аудио ответа приходит раньше, чем мы успеваем отреагировать на первый
        # байт — по этому флагу переключаем состояние ровно один раз за реплику.
        self._speaking = False
        self._transcript = ""
        self._cost = CostMeter(settings.openai_realtime_model)
        self._mic_batch = bytearray()
        self._batch_bytes = settings.mic_sample_rate * _SEND_BATCH_MS // 1000 * 2
        self._history: list[Turn] = []
        self._summary = None
        self._reconnecting = False
        self._refresh_task: asyncio.Task | None = None
        # Обмен «вопрос-ответ» копится целиком и ложится в память одной
        # парой. Порядок событий у Realtime этого не гарантирует: он
        # отвечает прямо на звук, а расшифровку вопроса Whisper досылает
        # параллельно и часто позже ответа. Записывая по факту прихода,
        # мы клали ответ раньше вопроса — в следующем разговоре модель
        # читала диалог со сдвигом и отвечала не на то, что спросили.
        self._pending_user: str | None = None
        self._pending_assistant: str | None = None
        self._user_ready = asyncio.Event()
        # Не озвучивать «секунду, переподключаюсь» чаще раза в
        # _RECONNECT_ANNOUNCE_MIN_GAP_S — см. константу.
        self._last_reconnect_announce = 0.0

    async def start(self, history: list[Turn], summary=None) -> None:
        # Запоминаем для переподключения: OpenAI сама рвёт сессию через час
        # («Your session hit the maximum duration of 60 minutes» — не ошибка,
        # а объявленный лимит), и без повторного старта колонка навсегда
        # остаётся с мёртвым соединением — слушает команды, но не отвечает.
        self._history = history
        self._summary = summary
        if self._refresh_task is not None:
            # Обновление таймера привязано к возрасту КОНКРЕТНОГО соединения —
            # старый отсчёт от предыдущего start() тут ни при чём.
            self._refresh_task.cancel()

        async def _connect() -> None:
            self._manager = self._client.realtime.connect(
                model=self._settings.openai_realtime_model
            )
            self._conn = await self._manager.__aenter__()
            await self._conn.session.update(
                session={
                    "type": "realtime",
                    "instructions": _build_instructions(
                        history, notes_tool.as_instructions(self._settings.notes_dir), summary
                    ),
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": _REALTIME_RATE},
                            "turn_detection": None,
                            "transcription": {"model": "whisper-1"},
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": _REALTIME_RATE},
                            "voice": self._settings.openai_voice,
                        },
                    },
                    "tools": _TOOLS,
                    # Без этого контекст растёт неограниченно, а Realtime считает
                    # входные токены за весь накопленный разговор на каждый ответ —
                    # десятая реплика стоит как десять первых.
                    "truncation": {
                        "type": "retention_ratio",
                        "retention_ratio": self._settings.realtime_retention_ratio,
                        "token_limits": {
                            "post_instructions": self._settings.realtime_context_tokens
                        },
                    },
                }
            )

        try:
            await asyncio.wait_for(_connect(), timeout=_CONNECT_TIMEOUT_S)
        except TimeoutError:
            log.error("подключение к Realtime не ответило за %.0f с", _CONNECT_TIMEOUT_S)
            if self._manager is not None:
                with contextlib.suppress(Exception):
                    await self._manager.__aexit__(None, None, None)
            self._manager = None
            self._conn = None
            raise

        self._recv_task = asyncio.create_task(self._recv_loop())
        self._refresh_task = asyncio.create_task(self._proactive_refresh())

    async def _flush_exchange(self) -> None:
        """Кладёт обмен в память в порядке «вопрос, потом ответ».

        Расшифровка вопроса приходит параллельно ответу и нередко позже
        него, поэтому здесь её недолго ждём. Не дождались — записываем
        только ответ: потерять реплику хуже, чем остаться без вопроса.
        """
        if self._pending_user is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._user_ready.wait(), timeout=_TRANSCRIPT_WAIT_S)

        user, assistant = self._pending_user, self._pending_assistant
        self._pending_user = self._pending_assistant = None
        self._user_ready.clear()

        if user:
            await self._cb.save_turn("user", user)
        if assistant:
            await self._cb.save_turn("assistant", assistant)

    async def begin_utterance(self) -> None:
        await self.barge_in()
        self._mic_batch.clear()
        self._transcript = ""
        # Прошлый обмен уже записан; на всякий случай не тащим его хвост
        # в новый — иначе вопрос от прошлой реплики склеится с этой.
        self._pending_user = None
        self._user_ready.clear()

    async def feed(self, pcm: bytes) -> None:
        if self._conn is None:
            return
        self._mic_batch.extend(pcm)
        if len(self._mic_batch) < self._batch_bytes:
            return
        await self._flush_mic()

    async def _flush_mic(self) -> None:
        """Отправляет накопленный микрофон одним сообщением."""
        if self._conn is None or not self._mic_batch:
            return
        batch = bytes(self._mic_batch)
        self._mic_batch.clear()
        # Пересчёт частоты на пачке — доли миллисекунды, в поток уводить
        # дороже, чем посчитать на месте.
        pcm24 = resample_pcm16(batch, self._settings.mic_sample_rate, _REALTIME_RATE)
        await self._conn.input_audio_buffer.append(audio=base64.b64encode(pcm24).decode("ascii"))

    async def end_utterance(self) -> None:
        if self._conn is None:
            # Соединение может оказаться пустым на мгновение: плановое
            # переподключение раз в час (_proactive_refresh) обнуляет его на
            # время пересборки. Если реплика закончилась ровно в этот зазор,
            # раньше это молча проглатывалось — сессия оставалась в LISTENING
            # навсегда, никто не звал turn_done(). Живой случай: колонка
            # простояла «слушаю» несколько часов до ручного вмешательства.
            log.warning("реплика закончилась во время переподключения к Realtime — реплика потеряна")
            # Если что-то (эхо колонки, шум в комнате) продолжает будить её
            # раз за разом, пока связи всё ещё нет, не озвучивать это на
            # каждый заход — живой случай: фраза зациклилась на минутном
            # интервале, пока соединение не поднималось вовсе.
            now = time.monotonic()
            if now - self._last_reconnect_announce >= _RECONNECT_ANNOUNCE_MIN_GAP_S:
                self._last_reconnect_announce = now
                with contextlib.suppress(Exception):
                    await self._ctx.speak("Секунду, переподключаюсь — повтори, пожалуйста.")
            await self._cb.turn_done()
            return
        # Хвост фразы ещё лежит в пачке — без этого пропадут последние
        # двести миллисекунд, а там обычно конец слова.
        await self._flush_mic()
        await self._cb.set_state(State.THINKING)
        await self._conn.input_audio_buffer.commit()
        await self._conn.response.create()

    async def barge_in(self) -> None:
        if self._conn is not None:
            # Отменять можно и когда отвечать нечему — сервер просто откажет.
            with contextlib.suppress(Exception):
                await self._conn.response.cancel()
            with contextlib.suppress(Exception):
                await self._conn.input_audio_buffer.clear()
        self._speaking = False
        await self._cb.drop_audio()

    async def close(self) -> None:
        if self._cost.turns:
            log.info(
                "разговор окончен: %.4f $ за %d реплик (в среднем %.4f $ на реплику)",
                self._cost.total_usd,
                self._cost.turns,
                self._cost.total_usd / self._cost.turns,
            )
        # Не переподключаться после закрытия сессии — иначе разговор с уже
        # отключившейся колонкой продолжит держать соединение с OpenAI.
        self._reconnecting = True
        if self._refresh_task is not None:
            self._refresh_task.cancel()
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
        if self._manager is not None:
            with contextlib.suppress(Exception):
                await self._manager.__aexit__(None, None, None)

    async def _recv_loop(self) -> None:
        assert self._conn is not None
        try:
            async for event in self._conn:
                # Уступаем цикл событий перед каждым сообщением. Облако шлёт
                # звук пачками по несколько десятков кусков, и они приходят
                # уже готовыми в буфере сокета: цикл прокручивался целиком,
                # ни разу не отдав управление. Внутри тоже уступить негде —
                # разбор события считает на месте, а микшер берёт свободный
                # замок, и тот возвращает управление сразу. В это время
                # отправщик, обязанный слать кадр каждые 20 мс, просто стоял:
                # работы у него на миллисекунду, но очередь до него не
                # доходила, и звук на колонке рвался с опозданием до 300 мс.
                await asyncio.sleep(0)
                try:
                    await self._on_event(event)
                except Exception:
                    log.exception("сбой обработки события realtime")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Соединение оборвалось (неверный ключ, сеть, квота) — исчерпав
            # встроенные ретраи SDK, оно просто закрывается.
            log.error("соединение с OpenAI Realtime оборвалось: %s", exc)
        else:
            # SDK на штатном закрытии (ConnectionClosedOK — именно так
            # закрывается сессия по истечении часа) просто завершает
            # генератор без исключения: `async for` заканчивается сам, и
            # это единственное место, где это видно. Ветка except её не
            # ловит — соединение оставалось «живым» на бумаге, а по факту
            # мёртвым: колонка слушала команды, но не отвечала.
            log.warning("соединение с OpenAI Realtime закрылось штатно (истёк час)")

        # Не даём сессии зависнуть в THINKING/SPEAKING навсегда: возвращаемся
        # в IDLE и считаем соединение мёртвым, чтобы feed()/end_utterance()
        # не пытались писать в закрытый сокет — независимо от того, было
        # это исключение или тихое штатное закрытие выше.
        self._conn = None
        self._speaking = False
        with contextlib.suppress(Exception):
            await self._cb.wait_drained()
        await self._cb.turn_done()
        if not self._reconnecting:
            asyncio.create_task(self._reconnect())

    async def _reconnect_with_retry(self) -> bool:
        """Несколько попыток поднять соединение подряд, с растущей паузой.

        И реактивный путь (после разрыва), и плановый (_proactive_refresh)
        раньше сдавались после первой же неудачи и ждали следующего повода —
        а для планового его больше не было: старое соединение уже закрыто,
        «штатный разрыв» ждать было нечего. Короткая сетевая заминка
        оставляла сессию мёртвой до следующего часового цикла.
        """
        for attempt in range(_RECONNECT_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(_RECONNECT_BACKOFF_S[attempt - 1])
            # Лог до попытки, а не только после неудачи: иначе тишина в
            # логе неотличима от «ещё не пробовали» — на живом разборе это
            # стоило немалого времени на угадывание, где именно застряло.
            log.info("попытка %d/%d поднять Realtime — начинаю", attempt + 1, _RECONNECT_ATTEMPTS)
            try:
                await self.start(self._history, self._summary)
                return True
            except Exception:
                log.exception(
                    "попытка %d/%d поднять Realtime не удалась", attempt + 1, _RECONNECT_ATTEMPTS
                )
        return False

    async def _reconnect(self) -> None:
        """Поднимает сессию заново после разрыва — час OpenAI держит её сам.

        Без этого колонка после часа разговора остаётся с мёртвым
        соединением: мигает состояниями, но ничего не отвечает, и со
        стороны это неотличимо от «не слышит».
        """
        if await self._reconnect_with_retry():
            log.info("соединение с OpenAI Realtime восстановлено")
        else:
            with contextlib.suppress(Exception):
                await self._ctx.speak("Голосовой сервис пока недоступен.")

    async def _proactive_refresh(self, delay_s: float | None = None) -> None:
        """Обновляет сессию заранее, не дожидаясь принудительного разрыва.

        Реактивный путь (`_recv_loop`) уже гарантирует правильность — он
        подхватит разрыв в любом случае. Но сам разрыв ничего не знает про
        разговор и может прийтись ровно на середину чьей-то фразы. Если
        обновиться заранее, в паузе, этого не заметят вовсе.

        `delay_s` параметризован ради тестов — в бою всегда берётся расчёт
        по объявленному часовому лимиту OpenAI.
        """
        if delay_s is None:
            delay_s = _SESSION_MAX_S - _REFRESH_MARGIN_S
        await asyncio.sleep(delay_s)
        if self._conn is None or self._reconnecting:
            # Уже мертво или уже пересобирается — реактивный путь разберётся.
            return
        if self._speaking:
            # Идёт озвучка ответа — прервать её было бы хуже, чем дождаться
            # штатного разрыва. Идеального сигнала «сейчас точно пауза» нет:
            # это лучшее доступное приближение, реактивный путь подстрахует.
            log.info("сессия Realtime скоро истечёт, но сейчас говорит — обновлюсь по разрыву")
            return

        log.info("сессия Realtime скоро истечёт — обновляю заранее, в паузе разговора")
        self._reconnecting = True
        # Обнуляем сразу, а не после пересборки: feed()/end_utterance() уже
        # проверяют "self._conn is None" и молча выходят — без этого они
        # могли бы попасть в окно между отменой старого соединения и
        # поднятием нового и упасть на закрывающемся сокете.
        old_task, self._recv_task = self._recv_task, None
        old_manager, self._manager = self._manager, None
        self._conn = None
        # Тот же урок, что и у start() (см. _CONNECT_TIMEOUT_S): закрытие
        # старого соединения — тоже сетевой вызов и тоже может зависнуть.
        # Живой случай: именно это молчаливо повисло на минуты. Важно: тут
        # НЕ ПОМОГАЕТ обернуть ожидание в asyncio.wait_for — если сама
        # задача проглатывает CancelledError и не завершается, wait_for
        # всё равно ждёт её настоящего конца, просто откладывая исключение
        # (проверено: зависает точно так же). Единственный надёжный выход —
        # не ждать синхронно вовсе: отменить и убрать в фоне, а к новому
        # соединению переходить сразу.
        if old_task is not None:
            old_task.cancel()
            asyncio.create_task(_drain_cancelled(old_task, "recv_task"))
        if old_manager is not None:
            asyncio.create_task(_drain_manager_exit(old_manager))
        log.info("старое соединение отправлено на закрытие в фоне — поднимаю новое")
        if await self._reconnect_with_retry():
            log.info("сессия Realtime обновлена заранее")
        else:
            log.error(
                "не удалось обновить сессию Realtime заранее за %d попыток", _RECONNECT_ATTEMPTS
            )
            with contextlib.suppress(Exception):
                await self._ctx.speak("Голосовой сервис пока недоступен.")
        self._reconnecting = False

    async def _on_event(self, event) -> None:
        etype = event.type
        if etype == "response.output_audio.delta":
            if not self._speaking:
                self._speaking = True
                await self._cb.set_state(State.SPEAKING)
            # Считаем на месте, а не в отдельном потоке. Замер на плате:
            # пересчёт стомиллисекундного куска — 0.7 мс, а облако шлёт их
            # десятками подряд, и на каждом переключении в поток и обратно
            # уходит больше, чем на самом расчёте.
            pcm48 = _decode_and_resample(
                event.delta, _REALTIME_RATE, self._settings.out_sample_rate
            )
            await self._cb.push_audio(pcm48)
        elif etype == "response.output_audio_transcript.delta":
            self._transcript += event.delta
            await self._cb.show_text(self._transcript)
        elif etype == "conversation.item.input_audio_transcription.completed":
            await self._cb.show_text(event.transcript)
            self._pending_user = event.transcript
            self._user_ready.set()
        elif etype == "response.done":
            self._cost.add(getattr(event.response, "usage", None))
            # Ответ, целиком состоящий из вызова инструмента, не значит, что
            # реплика закончилась — следом придёт ещё один response с озвучкой
            # результата, и вот на нём уже нужно гасить состояние.
            calls = [
                item
                for item in event.response.output
                if getattr(item, "type", None) == "function_call"
            ]
            if calls:
                for item in calls:
                    asyncio.create_task(self._run_tool(item.call_id, item.name, item.arguments))
                return
            self._speaking = False
            self._pending_assistant = self._transcript or None
            self._transcript = ""
            # Запись в память не должна задерживать возврат в IDLE: она
            # ждёт расшифровку вопроса, а колонка ждать не обязана.
            asyncio.create_task(self._flush_exchange())
            await self._cb.wait_drained()
            await self._cb.turn_done()
        elif etype == "error":
            log.warning("realtime сообщил об ошибке: %s", event.error)

    async def _run_tool(self, call_id: str, name: str, raw_args: str) -> None:
        log.info("инструмент %s(%s)", name, raw_args)
        # Запоминаем ИМЕННО этот объект соединения: пока dispatch() ждёт
        # (например, долгий web_search), сессия может успеть порваться и
        # переподключиться. self._conn is None этого не ловит — новое
        # соединение тоже не None, а call_id принадлежит старому и в новой
        # сессии неизвестен.
        conn = self._conn
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
        output = await dispatch(self._ctx, name, args)
        if conn is None or self._conn is not conn:
            log.warning("соединение сменилось во время %s — результат инструмента потерян", name)
            return
        await conn.conversation.item.create(
            item={"type": "function_call_output", "call_id": call_id, "output": output}
        )
        await conn.response.create()
