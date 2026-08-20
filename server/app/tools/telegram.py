"""Новости из публичных Telegram-каналов.

Читается веб-превью канала (`t.me/s/<канал>`) — обычная страница, без API,
токенов и авторизации. Работает только для публичных каналов; закрытые
отдают страницу-заглушку, и постов в ней просто не будет.

Пост в Telegram — не заголовок новости, а свободный текст: эмодзи, ссылки,
подпись канала, призывы подписаться. Всё это нужно вычистить, иначе колонка
прочитает вслух «подписаться стрелка вправо».
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

import httpx

log = logging.getLogger(__name__)

_POST_RE = re.compile(r'js-message_text[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)

# Строки-хвосты, которые есть почти в каждом посте и новостью не являются.
_NOISE_RE = re.compile(
    r"(підписатися|підписуйтесь|написати нам|прислать новость|надіслати новину|"
    r"наш канал|читати далі|источник|джерело|реклама|insider ua|@[\w_]+)",
    re.I,
)
# Эмодзи и прочие символы, которые синтезатор прочитает вслух как мусор.
_SYMBOLS_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿"
    # Селекторы вариации и «склейка» эмодзи: невидимы, но остаются после
    # вырезания самого значка и вылезают мусором в начале фразы.
    "\uFE0E\uFE0F\u200D\u2060\uFEFF]"
)

_HEADLINE_MAX_CHARS = 200
# Короче этого строка почти наверняка не новость, а подпись или эмодзи-строка.
# Слишком высокий порог рубит короткие заголовки вроде «Нанесены удары по НПЗ»,
# поэтому берём с запасом вниз — мусор всё равно отсеивается по словам.
_HEADLINE_MIN_CHARS = 18


async def get_channel_posts(channel: str, limit: int) -> list[str]:
    """Последние осмысленные посты канала, по одному предложению на пост."""
    url = f"https://t.me/s/{channel.lstrip('@')}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Без User-Agent Telegram отдаёт страницу без постов.
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("канал %s недоступен: %s", channel, exc)
        return []

    return await asyncio.to_thread(_extract, response.text, limit)


def _extract(page: str, limit: int) -> list[str]:
    posts = _POST_RE.findall(page)
    headlines: list[str] = []
    # Свежие посты в конце страницы, поэтому идём с хвоста.
    for raw in reversed(posts):
        text = _clean(raw)
        if text:
            headlines.append(text)
        if len(headlines) >= limit:
            break
    return headlines


def _clean(raw: str) -> str:
    text = _BR_RE.sub("\n", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _SYMBOLS_RE.sub(" ", text)

    # Берём первую содержательную строку: дальше обычно идут ссылки и
    # призывы подписаться, вслух они не нужны.
    for line in (ln.strip() for ln in text.split("\n")):
        line = " ".join(line.split())
        if len(line) < _HEADLINE_MIN_CHARS or _NOISE_RE.search(line):
            continue
        if len(line) > _HEADLINE_MAX_CHARS:
            # Режем по границе предложения, чтобы колонка не обрывалась
            # на полуслове.
            cut = line[:_HEADLINE_MAX_CHARS]
            end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            line = cut[: end + 1] if end > 60 else cut.rsplit(" ", 1)[0] + "…"
        # Та же чистка вёрстки, что и для лент СМИ: «(!)» синтезатор
        # читает как «скобка восклицательный знак скобка».
        from app.tools.news import _clean_title

        return _clean_title(line)
    return ""
