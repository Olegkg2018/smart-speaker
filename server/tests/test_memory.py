from app.memory import ConversationMemory


def test_empty_when_no_file(tmp_path):
    mem = ConversationMemory(tmp_path, "kitchen", limit=20)
    assert mem.turns == []


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
