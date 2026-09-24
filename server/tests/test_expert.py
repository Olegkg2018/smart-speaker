"""Инструмент ask_expert: сложный вопрос уходит текстовой модели."""

from __future__ import annotations

import types

import app.tools.expert as expert_mod
from app.memory import Turn
from app.tools.registry import tool_schemas


class _FakeResponses:
    def __init__(self, text="Потому что так устроено.", fail=False):
        self.calls: list[dict] = []
        self._text = text
        self._fail = fail

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise TimeoutError("нет ответа")
        return types.SimpleNamespace(
            output_text=self._text,
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def _patch_client(monkeypatch, responses):
    monkeypatch.setattr(
        expert_mod, "get_openai_client", lambda key: types.SimpleNamespace(responses=responses)
    )


async def test_expert_returns_clean_single_line_answer(monkeypatch):
    responses = _FakeResponses("  Потому что\n  так устроено.  ")
    _patch_client(monkeypatch, responses)

    answer = await expert_mod.ask_expert("key", "gpt-5.4-mini", "почему небо голубое", "", [], None)

    assert answer == "Потому что так устроено."
    assert responses.calls[0]["model"] == "gpt-5.4-mini"


async def test_expert_sees_recent_turns_and_summary(monkeypatch):
    responses = _FakeResponses()
    _patch_client(monkeypatch, responses)
    turns = [Turn("user", f"вопрос {i}") for i in range(10)]

    await expert_mod.ask_expert(
        "key", "m", "а почему?", "речь про погоду", turns, None, context_turns=3
    )

    prompt = responses.calls[0]["input"]
    assert "вопрос 9" in prompt and "вопрос 7" in prompt
    assert "вопрос 6" not in prompt, "старше context_turns не должно попадать"
    assert "речь про погоду" in prompt
    assert "а почему?" in prompt


async def test_expert_passes_reasoning_only_when_set(monkeypatch):
    responses = _FakeResponses()
    _patch_client(monkeypatch, responses)

    await expert_mod.ask_expert("key", "m", "вопрос", "", [], None)
    await expert_mod.ask_expert("key", "m", "вопрос", "", [], None, reasoning_effort="low")

    assert "reasoning" not in responses.calls[0]
    assert responses.calls[1]["reasoning"] == {"effort": "low"}


async def test_expert_failure_gives_a_short_apology_not_an_exception(monkeypatch):
    _patch_client(monkeypatch, _FakeResponses(fail=True))

    answer = await expert_mod.ask_expert("key", "m", "вопрос", "", [], None)

    assert "не отвечает" in answer


async def test_expert_needs_key_model_and_question(monkeypatch):
    responses = _FakeResponses()
    _patch_client(monkeypatch, responses)

    assert "не настроен" in await expert_mod.ask_expert("", "m", "вопрос", "", [], None)
    assert "не настроен" in await expert_mod.ask_expert("key", "", "вопрос", "", [], None)
    assert "не расслышал" in (await expert_mod.ask_expert("key", "m", "  ", "", [], None)).lower()
    assert responses.calls == []


def test_tool_is_hidden_from_the_model_until_expert_model_is_set():
    off = {t["name"] for t in tool_schemas(types.SimpleNamespace(expert_model=""))}
    on = {t["name"] for t in tool_schemas(types.SimpleNamespace(expert_model="gpt-5.4-mini"))}

    assert "ask_expert" not in off
    assert "ask_expert" in on
    assert off == on - {"ask_expert"}


def test_end_conversation_is_shown_only_to_realtime():
    """Окно продолжения есть только у Realtime — Claude закрывать нечего."""
    settings = types.SimpleNamespace(expert_model="gpt-5.4-mini")
    claude = {t["name"] for t in tool_schemas(settings)}
    realtime = {t["name"] for t in tool_schemas(settings, conversation_control=True)}

    assert "end_conversation" not in claude
    assert realtime == claude | {"end_conversation"}
