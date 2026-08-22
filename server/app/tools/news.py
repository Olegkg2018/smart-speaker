"""Новости из RSS-лент СМИ и публичных Telegram-каналов."""

from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger(__name__)

_HEADLINES = 3
# Сколько последних постов канала смотреть при поиске по теме: смотреть
# только _HEADLINES было ошибкой — если ни один из трёх последних постов не
# совпал, фильтр оставался ни с чем, даже если нужная новость есть на
# несколько постов раньше (частый случай: событие уже не первым постом).
_TELEGRAM_SCAN_LIMIT = 20
_TAG_RE = re.compile(r"<[^>]+>")

# Вёрстка заголовка, которую синтезатор читает буквально: «(!)» превращается
# в «скобка восклицательный знак скобка», кавычки-ёлочки дают паузы, а тире
# посреди фразы — паузу невпопад.
_DROP_RE = re.compile(r"\(\s*[!?]+\s*\)|[«»„“”\"]")
_DASH_RE = re.compile(r"\s+[—–]\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?])")
_REPEAT_COMMA_RE = re.compile(r"(,\s*){2,}")


def _clean_title(raw: str) -> str:
    """Заголовок как его произнесёт колонка, а не как он свёрстан на сайте."""
    title = _TAG_RE.sub(" ", raw)
    title = _DROP_RE.sub(" ", title)
    title = _DASH_RE.sub(", ", title)
    title = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", title)
    title = _REPEAT_COMMA_RE.sub(", ", title)
    return " ".join(title.split()).strip(" ,")


async def get_news(
    feeds: list[str],
    topic: str | None = None,
    telegram_channels: list[str] | None = None,
) -> str:
    channels = telegram_channels or []
    if not feeds and not channels:
        return "Ленты новостей не настроены."

    # Ленты и каналы опрашиваются разом: последовательно это добрая
    # полуминута ожидания, а колонка должна ответить голосом.
    rss_task = asyncio.to_thread(_collect, feeds, topic) if feeds else _nothing()
    tg_task = _collect_telegram(channels, topic) if channels else _nothing()
    rss_entries, tg_entries = await asyncio.gather(rss_task, tg_task)

    entries = _interleave([rss_entries, tg_entries])
    if not entries:
        return (
            f"По теме «{topic}» ничего не нашёл." if topic else "Не удалось загрузить новости."
        )
    return " ".join(f"{i}. {title}." for i, title in enumerate(entries, 1))


async def _nothing() -> list[str]:
    return []


async def _collect_telegram(channels: list[str], topic: str | None) -> list[str]:
    from app.tools.telegram import get_channel_posts

    # Без темы нужны только самые свежие посты; с темой — смотрим глубже,
    # иначе фильтр ищет среди трёх последних, где нужного поста может не быть.
    scan_limit = _TELEGRAM_SCAN_LIMIT if topic else _HEADLINES
    results = await asyncio.gather(
        *(get_channel_posts(ch, scan_limit) for ch in channels), return_exceptions=True
    )
    per_channel: list[list[str]] = []
    needle = topic.lower() if topic else None
    for channel, posts in zip(channels, results):
        if isinstance(posts, BaseException):
            log.warning("канал %s не прочитался: %s", channel, posts)
            continue
        if needle:
            posts = [p for p in posts if needle in p.lower()]
        if posts:
            per_channel.append(posts)
    return _interleave(per_channel)


def _interleave(groups: list[list[str]]) -> list[str]:
    """По одному заголовку из каждого источника, потом второй круг.

    Иначе первый же источник забивает всю выдачу своими новостями.
    """
    groups = [g for g in groups if g]
    if not groups:
        return []
    out: list[str] = []
    for i in range(_HEADLINES):
        for group in groups:
            if i < len(group):
                out.append(group[i])
                if len(out) >= _HEADLINES:
                    return out
    return out


def _collect(feeds: list[str], topic: str | None) -> list[str]:
    import feedparser

    needle = topic.lower() if topic else None
    per_feed: list[list[str]] = []

    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # feedparser бросает разное на битых лентах
            log.warning("лента %s недоступна: %s", url, exc)
            continue

        found: list[str] = []
        for entry in parsed.entries:
            title = _clean_title(getattr(entry, "title", ""))
            if not title:
                continue
            if needle and needle not in title.lower():
                continue
            found.append(title)
            if len(found) >= _HEADLINES:
                break
        if found:
            per_feed.append(found)

    if not per_feed:
        return []

    return _interleave(per_feed)
