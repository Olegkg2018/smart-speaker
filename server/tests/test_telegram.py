from app.tools.telegram import _clean, _extract

_PAGE = """
<div class="tgme_widget_message_text js-message_text">
⚡️ Нанесены удары по НПЗ в Нижнекамске<br/>Известно о погибших.<br/>
<a href="https://t.me/insiderUKR">INSIDER UA</a>
</div>
<div class="tgme_widget_message_text js-message_text">
😢 Наші з Київа на звʼязочку.<br/>⚠️ Удар за ударом, величезні пожежі.<br/>
📩 НАПИСАТИ НАМ ➡️ ПІДПИСАТИСЯ
</div>
"""


def test_extracts_newest_posts_first():
    posts = _extract(_PAGE, limit=2)
    assert len(posts) == 2
    # Свежие посты в конце страницы — их и берём первыми.
    assert "Київа" in posts[0]


def test_strips_emoji_and_markup():
    text = _clean("⚡️ Нанесены удары по НПЗ<br/>подробности")
    assert "⚡" not in text and "<" not in text
    assert "Нанесены удары" in text


def test_drops_subscribe_calls():
    # Такие строки колонка читала бы вслух как «подписаться стрелка вправо».
    assert _clean("📩 НАПИСАТИ НАМ ➡️ ПІДПИСАТИСЯ") == ""
    assert _clean('<a href="x">INSIDER UA</a>') == ""


def test_ignores_too_short_lines():
    assert _clean("Ок") == ""


def test_long_post_is_cut_at_sentence_end():
    long_text = "Первое предложение достаточно длинное. " + "Ещё текст. " * 40
    result = _clean(long_text)
    assert len(result) <= 201
    # Обрыв на полуслове в колонке звучит хуже, чем короткая фраза.
    assert result.endswith((".", "!", "?", "…"))


def test_empty_page_gives_nothing():
    assert _extract("<html></html>", limit=3) == []


def test_invisible_emoji_leftovers_are_stripped():
    """После вырезания значка остаётся невидимый селектор вариации."""
    text = _clean("⚡️ Нанесены удары по НПЗ в Нижнекамске")
    assert text.startswith("Нанесены"), repr(text)
    assert "️" not in text
