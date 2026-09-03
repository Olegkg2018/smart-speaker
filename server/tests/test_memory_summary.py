"""Свёртка вытесненной истории разговора отдельной, дешёвой моделью.

Сводка структурная: поля, а не проза. Прозой сюда однажды попало
«необходимо запускать музыку, когда она вернётся» — модель прочла это
как указание и включила ABBA вместо ответа про погоду.
"""

from app import memory_summary
from app.memory import Turn


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content, captured):
        self._content = content
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.append(kwargs)
        return _FakeResponse(self._content)


class _FakeClient:
    def __init__(self, content, captured):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(content, captured)})()


def _patch(monkeypatch, content='{"человек": ["Иван"]}'):
    captured: list = []
    monkeypatch.setattr(
        memory_summary, "get_openai_client", lambda api_key: _FakeClient(content, captured)
    )
    return captured


async def test_no_api_key_returns_prior_summary_unchanged(monkeypatch):
    captured = _patch(monkeypatch)
    prior = {"человек": ["Иван"]}
    result = await memory_summary.fold_in("", "gpt-4o-mini", prior, [Turn("user", "привет")])
    assert result["человек"] == ["Иван"]
    assert captured == [], "без ключа сетевого вызова быть не должно"


async def test_no_evicted_turns_skips_the_call(monkeypatch):
    captured = _patch(monkeypatch)
    prior = {"человек": ["Иван"]}
    result = await memory_summary.fold_in("sk-test", "gpt-4o-mini", prior, [])
    assert result["человек"] == ["Иван"]
    assert captured == [], "нечего сворачивать — незачем звать модель"


async def test_folds_evicted_turns_into_new_summary(monkeypatch):
    captured = _patch(
        monkeypatch, content='{"договорённости": ["поливает цветы по средам"]}'
    )
    result = await memory_summary.fold_in(
        "sk-test", "gpt-4o-mini", None, [Turn("user", "полей цветы в среду")]
    )
    assert result["договорённости"] == ["поливает цветы по средам"]
    assert len(captured) == 1
    assert captured[0]["model"] == "gpt-4o-mini"
    assert "полей цветы" in captured[0]["messages"][0]["content"]


async def test_api_failure_keeps_prior_summary(monkeypatch):
    class _BrokenCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("нет сети")

    class _BrokenClient:
        chat = type("Chat", (), {"completions": _BrokenCompletions()})()

    monkeypatch.setattr(memory_summary, "get_openai_client", lambda api_key: _BrokenClient())

    prior = {"человек": ["Иван"]}
    result = await memory_summary.fold_in(
        "sk-test", "gpt-4o-mini", prior, [Turn("user", "привет")]
    )
    assert result["человек"] == ["Иван"]


async def test_empty_model_reply_keeps_prior_summary(monkeypatch):
    _patch(monkeypatch, content="")
    prior = {"человек": ["Иван"]}
    result = await memory_summary.fold_in(
        "sk-test", "gpt-4o-mini", prior, [Turn("user", "привет")]
    )
    assert result["человек"] == ["Иван"]
