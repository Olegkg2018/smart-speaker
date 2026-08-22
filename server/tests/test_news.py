import app.tools.telegram as telegram_module
from app.tools.news import _HEADLINES, _TELEGRAM_SCAN_LIMIT, _clean_title, _collect_telegram, _interleave


def test_strips_typography_read_aloud_literally():
    """«(!)» синтезатор произносит как «скобка восклицательный знак скобка»."""
    assert "(" not in _clean_title("Повреждён каждый третий (!) дом")


def test_dash_becomes_comma():
    # Длинное тире даёт паузу невпопад посреди фразы.
    assert _clean_title("Заголовок — пояснение") == "Заголовок, пояснение"


def test_quotes_are_dropped():
    assert "«" not in _clean_title("«Цитата» в заголовке")


def test_no_double_commas_after_cleanup():
    result = _clean_title("Третий (!) дом , — Кличко")
    assert ",," not in result and ", ," not in result


def test_space_before_comma_is_removed():
    assert _clean_title("Слово , дальше") == "Слово, дальше"


def test_plain_title_untouched():
    assert _clean_title("Обычный заголовок без мусора") == "Обычный заголовок без мусора"


def test_html_tags_removed():
    assert _clean_title("<b>Жирный</b> заголовок") == "Жирный заголовок"


def test_interleave_takes_one_from_each_source():
    """Иначе первая же лента забивает выдачу своими новостями."""
    result = _interleave([["а1", "а2", "а3"], ["б1", "б2"]])
    assert result[0] == "а1" and result[1] == "б1"


def test_interleave_skips_empty_sources():
    assert _interleave([[], ["б1"], []]) == ["б1"]


def test_interleave_of_nothing():
    assert _interleave([[], []]) == []


async def test_topic_search_scans_more_posts_than_it_shows(monkeypatch):
    """Раньше фильтр по теме смотрел только на _HEADLINES последних постов —
    если нужная новость не попадала в тройку самых свежих (частый случай:
    событие уже не первым постом), поиск находил пусто, хотя новость была."""
    seen_limits = []

    async def fake_get_channel_posts(channel, limit):
        seen_limits.append(limit)
        return []

    monkeypatch.setattr(telegram_module, "get_channel_posts", fake_get_channel_posts)

    await _collect_telegram(["insiderUKR"], "Кривой Рог")
    assert seen_limits == [_TELEGRAM_SCAN_LIMIT]
    assert _TELEGRAM_SCAN_LIMIT > _HEADLINES


async def test_no_topic_scans_only_headline_count(monkeypatch):
    seen_limits = []

    async def fake_get_channel_posts(channel, limit):
        seen_limits.append(limit)
        return []

    monkeypatch.setattr(telegram_module, "get_channel_posts", fake_get_channel_posts)

    await _collect_telegram(["insiderUKR"], None)
    assert seen_limits == [_HEADLINES]
