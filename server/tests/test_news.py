from app.tools.news import _clean_title, _interleave


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
