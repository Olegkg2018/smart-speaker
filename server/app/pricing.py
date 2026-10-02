"""Подсчёт стоимости разговора по токенам Realtime API.

Кабинет OpenAI показывает расход с задержкой и требует отдельных прав на
чтение, поэтому считаем сами: каждый ответ модели приносит usage, а тарифы
известны. Это даёт цену прямо в логах, сразу после реплики.

Тарифы — доллары за миллион токенов, по прайсу OpenAI на август 2026.
Аудио дороже текста примерно в восемь раз, поэтому именно оно определяет
счёт: пересказ той же фразы текстом стоил бы копейки.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rates:
    text_in: float
    text_in_cached: float
    audio_in: float
    audio_in_cached: float
    text_out: float
    audio_out: float


# Ключ — начало имени модели: в API попадают и версии с датой.
_PRICES: dict[str, Rates] = {
    "gpt-realtime-2.1-mini": Rates(0.6, 0.06, 10.0, 0.3, 2.4, 20.0),
    "gpt-realtime-2.1": Rates(4.0, 0.4, 32.0, 0.4, 24.0, 64.0),
    "gpt-realtime-2": Rates(4.0, 0.4, 32.0, 0.4, 24.0, 64.0),
    "gpt-realtime": Rates(4.0, 0.4, 32.0, 0.4, 24.0, 64.0),
}


def rates_for(model: str) -> Rates | None:
    # Сначала длинные имена: "gpt-realtime-2.1-mini" не должен совпасть
    # с "gpt-realtime" и посчитаться по втрое большему тарифу.
    for prefix in sorted(_PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return _PRICES[prefix]
    return None


_LEDGER_KEEP_DAYS = 400


class CostLedger:
    """Расход по дням на диске: переживает пересоздание контейнера.

    Сессия копит итог только в памяти (CostMeter), а ответ на «сколько
    потратили» нужен за сутки и за месяц. Один файл, ключ — дата.
    """

    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, usd: float, today: date | None = None) -> None:
        if usd <= 0:
            return
        today = today or date.today()
        data = self._load()
        day = data.get(today.isoformat()) or {}
        data[today.isoformat()] = {
            "usd": round(float(day.get("usd", 0.0)) + usd, 6),
            "turns": int(day.get("turns", 0)) + 1,
        }
        cutoff = (today - timedelta(days=_LEDGER_KEEP_DAYS)).isoformat()
        data = {k: v for k, v in data.items() if k >= cutoff}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("не удалось записать учёт расходов: %s", exc)

    def summary(self, today: date | None = None) -> dict:
        """Сегодня, текущий месяц и последние 14 дней для страницы."""
        today = today or date.today()
        data = self._load()
        month = today.strftime("%Y-%m")
        month_usd = sum(float(v.get("usd", 0)) for k, v in data.items() if k.startswith(month))
        today_row = data.get(today.isoformat()) or {}
        days = []
        for i in range(13, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            row = data.get(d) or {}
            days.append({"date": d, "usd": float(row.get("usd", 0.0)),
                         "turns": int(row.get("turns", 0))})
        return {
            "today_usd": float(today_row.get("usd", 0.0)),
            "today_turns": int(today_row.get("turns", 0)),
            "month_usd": round(month_usd, 6),
            "days": days,
        }


class CostMeter:
    """Копит стоимость сессии и умеет показать её словами."""

    def __init__(self, model: str, ledger: CostLedger | None = None):
        self._model = model
        self._ledger = ledger
        self._rates = rates_for(model)
        self.total_usd = 0.0
        self.turns = 0
        if self._rates is None:
            log.info("тариф для модели «%s» неизвестен — цену не считаю", model)

    def add(self, usage) -> float:
        """Считает стоимость одного ответа. Возвращает её в долларах."""
        if self._rates is None or usage is None:
            return 0.0

        # Форма usage у разных версий SDK отличается: где-то объект, где-то
        # словарь, а разбивка по модальностям может отсутствовать вовсе.
        def get(obj, *names, default=0):
            for name in names:
                if obj is None:
                    return default
                obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            return default if obj is None else obj

        in_details = get(usage, "input_token_details", default=None)
        out_details = get(usage, "output_token_details", default=None)
        cached_details = get(in_details, "cached_tokens_details", default=None)

        audio_in = get(in_details, "audio_tokens")
        text_in = get(in_details, "text_tokens")
        cached_audio = get(cached_details, "audio_tokens")
        cached_text = get(cached_details, "text_tokens")
        audio_out = get(out_details, "audio_tokens")
        text_out = get(out_details, "text_tokens")

        # Кэшированные токены API считает и в общем входе тоже — вычитаем,
        # иначе они попадут в счёт дважды, да ещё и по полному тарифу.
        audio_in = max(0, audio_in - cached_audio)
        text_in = max(0, text_in - cached_text)

        r = self._rates
        cost = (
            text_in * r.text_in
            + cached_text * r.text_in_cached
            + audio_in * r.audio_in
            + cached_audio * r.audio_in_cached
            + text_out * r.text_out
            + audio_out * r.audio_out
        ) / 1_000_000

        self.total_usd += cost
        self.turns += 1
        if self._ledger is not None:
            self._ledger.record(cost)
        log.info(
            "реплика: %.4f $ (вход %d аудио + %d текст, кэш %d; выход %d аудио + %d текст) "
            "| всего за сессию %.4f $ за %d реплик",
            cost, audio_in, text_in, cached_audio, audio_out, text_out,
            self.total_usd, self.turns,
        )
        return cost
