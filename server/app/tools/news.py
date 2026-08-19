"""Новости из RSS-лент."""

from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger(__name__)

_HEADLINES = 3
_TAG_RE = re.compile(r"<[^>]+>")


async def get_news(feeds: list[str], topic: str | None = None) -> str:
    if not feeds:
        return "Ленты новостей не настроены."
    entries = await asyncio.to_thread(_collect, feeds, topic)
    if not entries:
        return (
            f"По теме «{topic}» ничего не нашёл." if topic else "Не удалось загрузить новости."
        )
    return " ".join(f"{i}. {title}." for i, title in enumerate(entries, 1))


def _collect(feeds: list[str], topic: str | None) -> list[str]:
    import feedparser

    needle = topic.lower() if topic else None
    titles: list[str] = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # feedparser бросает разное на битых лентах
            log.warning("лента %s недоступна: %s", url, exc)
            continue
        for entry in parsed.entries:
            title = _TAG_RE.sub("", getattr(entry, "title", "")).strip()
            if not title:
                continue
            if needle and needle not in title.lower():
                continue
            titles.append(title)
            if len(titles) >= _HEADLINES:
                return titles
    return titles
