"""Свёртка вытесненной истории разговора отдельной, дешёвой моделью."""

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


def _patch(monkeypatch, content="новая сводка"):
    captured: list = []
    monkeypatch.setattr(memory_summary, "AsyncOpenAI", lambda *a, **kw: _FakeClient(content, captured))
    return captured


async def test_no_api_key_returns_prior_summary_unchanged(monkeypatch):
    captured = _patch(monkeypatch)
    result = await memory_summary.fold_in("", "gpt-4o-mini", "старая сводка", [Turn("user", "привет")])
    assert result == "старая сводка"
    assert captured == [], "без ключа сетевого вызова быть не должно"


async def test_no_evicted_turns_skips_the_call(monkeypatch):
    captured = _patch(monkeypatch)
    result = await memory_summary.fold_in("sk-test", "gpt-4o-mini", "старая сводка", [])
    assert result == "старая сводка"
    assert captured == [], "нечего сворачивать — незачем звать модель"


async def test_folds_evicted_turns_into_new_summary(monkeypatch):
    captured = _patch(monkeypatch, content="Иван просил поливать цветы по средам.")
    result = await memory_summary.fold_in(
        "sk-test", "gpt-4o-mini", "", [Turn("user", "полей цветы в среду")]
    )
    assert result == "Иван просил поливать цветы по средам."
    assert len(captured) == 1
    assert captured[0]["model"] == "gpt-4o-mini"
    assert "полей цветы" in captured[0]["messages"][0]["content"]


async def test_api_failure_keeps_prior_summary(monkeypatch):
    class _BrokenCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("нет сети")

    class _BrokenClient:
        chat = type("Chat", (), {"completions": _BrokenCompletions()})()

    monkeypatch.setattr(memory_summary, "AsyncOpenAI", lambda *a, **kw: _BrokenClient())

    result = await memory_summary.fold_in(
        "sk-test", "gpt-4o-mini", "старая сводка", [Turn("user", "привет")]
    )
    assert result == "старая сводка"


async def test_empty_model_reply_keeps_prior_summary(monkeypatch):
    _patch(monkeypatch, content="")
    result = await memory_summary.fold_in(
        "sk-test", "gpt-4o-mini", "старая сводка", [Turn("user", "привет")]
    )
    assert result == "старая сводка"
