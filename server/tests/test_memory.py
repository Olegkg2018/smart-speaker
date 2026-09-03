import json

from app.memory import ConversationMemory


def test_empty_when_no_file(tmp_path):
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    assert mem.turns == []
    # Сводка теперь поля, а не строка: пустая — пустой объект.
    assert not mem.summary


def test_append_and_reload_roundtrip(tmp_path):
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    mem.append("user", "какая погода")
    mem.append("assistant", "Пасмурно, восемнадцать градусов.")

    reloaded = ConversationMemory(tmp_path, "kitchen", limit=20)
    assert [(t.role, t.text) for t in reloaded.turns] == [
        ("user", "какая погода"),
        ("assistant", "Пасмурно, восемнадцать градусов."),
    ]


def test_trims_to_limit(tmp_path):
    mem = ConversationMemory(tmp_path, "kitchen", limit=3)
    for i in range(5):
        mem.append("user", f"реплика {i}")
    assert [t.text for t in mem.turns] == ["реплика 2", "реплика 3", "реплика 4"]


def test_append_returns_evicted_turns_for_summarizing():
    """Раньше вытесненное молча пропадало — теперь его должны свернуть в сводку."""
    mem = ConversationMemory.__new__(ConversationMemory)
    mem._path = None
    mem._limit = 3
    mem._turns = []
    mem._summary = ""
    mem._save = lambda: None  # диск здесь не при чём

    assert mem.append("user", "реплика 0") == []
    assert mem.append("user", "реплика 1") == []
    assert mem.append("user", "реплика 2") == []
    evicted = mem.append("user", "реплика 3")
    assert [t.text for t in evicted] == ["реплика 0"]
    assert [t.text for t in mem.turns] == ["реплика 1", "реплика 2", "реплика 3"]


def test_set_summary_persists(tmp_path):
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    mem.set_summary("Иван просил поливать цветы по средам.")

    reloaded = ConversationMemory(tmp_path, "kitchen", limit=20)
    assert reloaded.summary == "Иван просил поливать цветы по средам."


def test_old_plain_list_format_still_loads(tmp_path):
    """Файлы, записанные до появления сводки, — голый список реплик."""
    path = tmp_path / "kitchen.json"
    path.write_text(
        json.dumps([{"role": "user", "text": "привет"}], ensure_ascii=False),
        encoding="utf-8",
    )
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    assert [t.text for t in mem.turns] == ["привет"]
    assert not mem.summary


def test_devices_are_isolated(tmp_path):
    kitchen = ConversationMemory(tmp_path, "kitchen", limit=20)
    kitchen.append("user", "привет с кухни")
    bedroom = ConversationMemory(tmp_path, "bedroom", limit=20)
    assert bedroom.turns == []


def test_empty_text_is_ignored(tmp_path):
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    mem.append("user", "   ")
    assert mem.turns == []


def test_corrupt_file_is_ignored_not_raised(tmp_path):
    path = tmp_path / "kitchen.json"
    path.write_text("не json вообще", encoding="utf-8")
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    assert mem.turns == []
