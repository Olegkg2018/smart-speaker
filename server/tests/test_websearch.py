from app.tools.websearch import _strip_citations


def test_strips_markdown_citation_in_parens():
    """Поиск возвращает ссылки прямо посреди фразы, вслух это набор букв."""
    text = _strip_citations("Курс 44,70 грн. ([rbc.ua](https://rbc.ua/news))")
    assert "http" not in text and "[" not in text
    assert "44,70" in text


def test_keeps_link_text_when_it_is_part_of_the_sentence():
    assert _strip_citations("По данным [НБУ](https://bank.gov.ua) курс вырос") == (
        "По данным НБУ курс вырос"
    )


def test_plain_answer_untouched():
    assert _strip_citations("Курс сорок четыре гривны") == "Курс сорок четыре гривны"


def test_collapses_leftover_spaces():
    assert "  " not in _strip_citations("Ответ ([a](b))  с   пробелами")
