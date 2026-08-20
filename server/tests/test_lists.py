import pytest

from app.tools import lists


async def test_add_then_read(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    await lists.add_to_list(tmp_path, "покупки", "хлеб")
    result = await lists.read_list(tmp_path, "покупки")
    assert "молоко" in result and "хлеб" in result


async def test_list_survives_restart(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    # Новый вызов читает с диска — как после перезапуска сервера.
    assert "молоко" in await lists.read_list(tmp_path, "покупки")


async def test_duplicate_is_reported_not_added(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    again = await lists.add_to_list(tmp_path, "покупки", "Молоко")
    assert "уже" in again.lower()
    # Дважды одно и то же в списке покупок только путает.
    assert (await lists.read_list(tmp_path, "покупки")).count("олоко") == 1


async def test_empty_list_says_so(tmp_path):
    assert "пуст" in (await lists.read_list(tmp_path, "покупки")).lower()


async def test_remove_prefers_exact_match(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко кокосовое")
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    await lists.remove_from_list(tmp_path, "покупки", "молоко")
    left = await lists.read_list(tmp_path, "покупки")
    # Должно уйти именно «молоко», а не то, что просто его содержит.
    assert "кокосовое" in left


async def test_remove_falls_back_to_partial_match(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко кокосовое")
    result = await lists.remove_from_list(tmp_path, "покупки", "кокосовое")
    assert "убрал" in result.lower()
    assert "пуст" in (await lists.read_list(tmp_path, "покупки")).lower()


async def test_remove_missing_item_is_honest(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "хлеб")
    result = await lists.remove_from_list(tmp_path, "покупки", "молоко")
    assert "не нашёл" in result.lower()


async def test_clear_empties_the_list(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    await lists.clear_list(tmp_path, "покупки")
    assert "пуст" in (await lists.read_list(tmp_path, "покупки")).lower()


async def test_lists_are_independent(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    await lists.add_to_list(tmp_path, "дела", "позвонить маме")
    assert "молоко" not in await lists.read_list(tmp_path, "дела")


async def test_which_lists_names_them(tmp_path):
    await lists.add_to_list(tmp_path, "покупки", "молоко")
    await lists.add_to_list(tmp_path, "дела", "позвонить")
    result = await lists.which_lists(tmp_path)
    assert "покупки" in result and "дела" in result


async def test_no_lists_yet(tmp_path):
    assert "нет" in (await lists.which_lists(tmp_path)).lower()


@pytest.mark.parametrize(
    "name", ["../побег", "покупки/../..", "спи*ски?", "  ПОКУПКИ  "]
)
async def test_dictated_name_cannot_escape_the_directory(tmp_path, name):
    """Имя списка диктуют голосом — в него попадает что угодно."""
    await lists.add_to_list(tmp_path, name, "молоко")
    written = list(tmp_path.rglob("*.json"))
    assert written, "список должен был сохраниться"
    for path in written:
        assert path.parent == tmp_path, f"файл ушёл из папки: {path}"


async def test_long_list_is_shortened_for_speaking(tmp_path):
    for i in range(20):
        await lists.add_to_list(tmp_path, "покупки", f"товар {i}")
    result = await lists.read_list(tmp_path, "покупки")
    # Двадцать пунктов подряд вслух — это невыносимо.
    assert "и ещё" in result


async def test_empty_item_is_rejected(tmp_path):
    result = await lists.add_to_list(tmp_path, "покупки", "   ")
    assert "не расслышал" in result.lower()


async def test_broken_file_does_not_crash(tmp_path):
    path = lists._list_path(tmp_path, "покупки")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("не json", encoding="utf-8")
    assert "пуст" in (await lists.read_list(tmp_path, "покупки")).lower()


async def test_load_items_returns_raw_list(tmp_path):
    """Плейлист проигрывает пункты по очереди, а не зачитывает вслух."""
    await lists.add_to_list(tmp_path, "мой плейлист", "Кино — Группа крови")
    await lists.add_to_list(tmp_path, "мой плейлист", "Пикник — Иероглиф")
    items = lists.load_items(tmp_path, "мой плейлист")
    assert items == ["Кино — Группа крови", "Пикник — Иероглиф"]


def test_load_items_on_missing_list(tmp_path):
    assert lists.load_items(tmp_path, "нет такого") == []
