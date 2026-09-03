"""Сводка хранится полями, а не прозой.

Живой случай, ради которого это и переделано: в свободный текст сводки
затесалось «необходимо продолжать или запускать музыку, когда она
вернётся». Модель прочитала это как поручение и на вопрос о погоде
включила ABBA. Факт, разложенный по полю, поручением не выглядит.
"""

from app import memory_summary
from app.memory import Turn


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Msg(content)})()]


def _patch(monkeypatch, content):
    captured: list = []

    class _Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _Resp(content)

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(memory_summary, "get_openai_client", lambda k: _Client())
    return captured


# ---------- нормализация ----------


def test_old_plain_text_summary_is_not_lost():
    """Файлы, записанные до этой правки, хранят сводку строкой."""
    got = memory_summary.normalize("Вика любит ABBA и Queen")
    assert got["человек"] == ["Вика любит ABBA и Queen"]
    assert got["предпочтения"] == []


def test_missing_and_broken_input_gives_empty_schema():
    for junk in (None, "", 42, [], {"чужое поле": ["x"]}):
        got = memory_summary.normalize(junk)
        assert set(got) == set(memory_summary.FIELDS)
        assert all(v == [] for v in got.values()), junk


def test_single_string_in_field_becomes_list():
    got = memory_summary.normalize({"человек": "Вика"})
    assert got["человек"] == ["Вика"]


def test_duplicates_and_overlong_items_are_trimmed():
    long = "я" * 500
    got = memory_summary.normalize({"быт": ["дом", "дом", long]})
    assert got["быт"][:1] == ["дом"], "дубликат должен схлопнуться"
    assert len(got["быт"][-1]) <= 160, "слишком длинный пункт должен обрезаться"


def test_field_cannot_grow_without_limit():
    got = memory_summary.normalize({"предпочтения": [f"факт {i}" for i in range(50)]})
    assert len(got["предпочтения"]) <= 8


# ---------- подстановка в инструкции ----------


def test_as_text_labels_fields_and_skips_empty():
    text = memory_summary.as_text(
        {"человек": ["Вика"], "предпочтения": ["любит ABBA"], "быт": []}
    )
    assert "человек: Вика" in text
    assert "предпочтения: любит ABBA" in text
    assert "быт" not in text, "пустые поля не должны попадать в инструкции"


def test_as_text_of_nothing_is_empty():
    assert memory_summary.as_text(None) == ""
    assert memory_summary.as_text({}) == ""
    assert memory_summary.is_empty({"человек": []})


# ---------- сама свёртка ----------


async def test_model_is_asked_for_json(monkeypatch):
    captured = _patch(monkeypatch, '{"человек": ["Вика"]}')
    await memory_summary.fold_in("sk", "gpt-4o-mini", None, [Turn("user", "я Вика")])
    assert captured[0]["response_format"] == {"type": "json_object"}


async def test_non_json_reply_keeps_prior(monkeypatch):
    _patch(monkeypatch, "извините, не могу")
    prior = {"человек": ["Вика"]}
    got = await memory_summary.fold_in("sk", "gpt-4o-mini", prior, [Turn("user", "…")])
    assert got["человек"] == ["Вика"], "мусор от модели не должен стирать накопленное"


async def test_empty_json_reply_keeps_prior(monkeypatch):
    _patch(monkeypatch, "{}")
    prior = {"предпочтения": ["любит ABBA"]}
    got = await memory_summary.fold_in("sk", "gpt-4o-mini", prior, [Turn("user", "…")])
    assert got["предпочтения"] == ["любит ABBA"]


async def test_prior_summary_is_shown_to_the_model(monkeypatch):
    captured = _patch(monkeypatch, '{"человек": ["Вика"]}')
    await memory_summary.fold_in(
        "sk", "gpt-4o-mini", {"человек": ["Вика"]}, [Turn("user", "привет")]
    )
    prompt = captured[0]["messages"][0]["content"]
    assert "Вика" in prompt, "модель должна видеть прежнюю заметку, чтобы её дополнить"


async def test_prompt_warns_against_commands_and_guessing(monkeypatch):
    """Два правила в промпте держат ровно те грабли, на которые уже наступали."""
    captured = _patch(monkeypatch, '{"человек": []}')
    await memory_summary.fold_in("sk", "gpt-4o-mini", None, [Turn("user", "х")])
    prompt = captured[0]["messages"][0]["content"]
    assert "не команда" in prompt
    assert "криво распознан" in prompt
