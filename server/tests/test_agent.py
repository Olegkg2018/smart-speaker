"""Ручной цикл tool use в agent.py: tool_use → tool_result → повторный запрос.

Код не работает при VOICE_PROVIDER=openai_realtime, поэтому сломается тихо и
обнаружится в день, когда бэкенд переключат. Клиент Anthropic подменён
заглушкой, стриминг — готовыми событиями.
"""

import types

import app.agent as agent_mod
from app.agent import Agent
from app.memory import Turn


def _delta(text):
    return types.SimpleNamespace(
        type="content_block_delta",
        delta=types.SimpleNamespace(type="text_delta", text=text),
    )


def _message(stop_reason, content):
    return types.SimpleNamespace(stop_reason=stop_reason, content=content)


class _Stream:
    def __init__(self, events, message):
        self._events, self._message = events, message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def get_final_message(self):
        return self._message


class _Messages:
    def __init__(self, streams):
        self._streams = list(streams)
        self.requests: list[dict] = []

    def stream(self, **kwargs):
        # Список сообщений меняется после запроса — снимаем копию сейчас.
        kwargs["messages"] = list(kwargs["messages"])
        self.requests.append(kwargs)
        return self._streams.pop(0)


def _agent(streams, monkeypatch, dispatched):
    agent = Agent.__new__(Agent)
    agent._settings = types.SimpleNamespace(
        model="m", max_tokens=100, effort="low", max_history_turns=20, notes_dir=None,
    )
    agent._ctx = object()
    agent._history = []
    agent._summary = None
    agent._notes = lambda: ""
    agent._client = types.SimpleNamespace(messages=_Messages(streams))

    async def fake_dispatch(ctx, name, args):
        dispatched.append((name, args))
        return "+20, ясно"

    monkeypatch.setattr(agent_mod, "dispatch", fake_dispatch)
    monkeypatch.setattr(agent_mod, "tool_schemas", lambda settings: [])
    return agent


async def test_tool_use_result_then_second_request(monkeypatch):
    tool_block = types.SimpleNamespace(
        type="tool_use", id="tu_1", name="get_weather", input={"period": "tomorrow"}
    )
    first = _Stream([], _message("tool_use", [tool_block]))
    second = _Stream([_delta("Завтра будет тепло. ")], _message("end_turn", []))
    dispatched: list = []
    agent = _agent([first, second], monkeypatch, dispatched)
    spoken: list[str] = []

    async def on_sentence(s):
        spoken.append(s)

    await agent.respond("какая погода завтра", on_sentence)

    assert dispatched == [("get_weather", {"period": "tomorrow"})]
    assert spoken and "Завтра будет тепло" in "".join(spoken)
    second_request = agent._client.messages.requests[1]["messages"]
    # user → assistant(tool_use) → user(tool_result)
    assert [m["role"] for m in second_request] == ["user", "assistant", "user"]
    assert second_request[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "+20, ясно"}
    ]


async def test_refusal_speaks_apology_and_stops(monkeypatch):
    refused = _Stream([], _message("refusal", []))
    agent = _agent([refused], monkeypatch, [])
    spoken: list[str] = []

    async def on_sentence(s):
        spoken.append(s)

    await agent.respond("что-то", on_sentence)

    assert spoken == ["Извини, на это я ответить не могу."]
    assert len(agent._client.messages.requests) == 1


def test_trim_history_never_starts_with_orphan_tool_result():
    agent = Agent.__new__(Agent)
    agent._settings = types.SimpleNamespace(max_history_turns=3)
    agent._history = [
        {"role": "user", "content": "старый вопрос"},
        {"role": "assistant", "content": [object()]},
        {"role": "user", "content": [{"type": "tool_result"}]},
        {"role": "assistant", "content": "ответ"},
        {"role": "user", "content": "новый вопрос"},
    ]
    agent._trim_history()
    assert agent._history[0] == {"role": "user", "content": "новый вопрос"}


def test_seed_history_keeps_text_only():
    agent = Agent.__new__(Agent)
    agent._settings = types.SimpleNamespace(max_history_turns=10)
    agent._history = []
    agent.seed_history([Turn("user", "привет"), Turn("assistant", "здравствуй")])
    assert agent._history == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуй"},
    ]
