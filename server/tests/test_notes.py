from app.tools import notes


async def test_remember_then_recall(tmp_path):
    await notes.remember(tmp_path, "жену хозяина зовут Марина")
    assert "Марина" in await notes.list_notes(tmp_path)


async def test_note_survives_restart(tmp_path):
    await notes.remember(tmp_path, "в доме не едят мясо")
    # Читаем заново — как после перезапуска сервера.
    assert notes.load(tmp_path) == ["в доме не едят мясо"]


async def test_duplicate_is_not_stored_twice(tmp_path):
    await notes.remember(tmp_path, "не едят мясо")
    again = await notes.remember(tmp_path, "Не Едят Мясо")
    assert "уже" in again.lower()
    assert len(notes.load(tmp_path)) == 1


async def test_forget_by_word(tmp_path):
    await notes.remember(tmp_path, "в доме не едят мясо")
    await notes.remember(tmp_path, "жену зовут Марина")
    await notes.forget(tmp_path, "мясо")
    left = notes.load(tmp_path)
    assert len(left) == 1 and "Марина" in left[0]


async def test_forget_unknown_is_honest(tmp_path):
    result = await notes.forget(tmp_path, "чего-то")
    assert "не помнил" in result.lower()


async def test_empty_note_rejected(tmp_path):
    assert "не расслышал" in (await notes.remember(tmp_path, "   ")).lower()


async def test_long_note_is_trimmed(tmp_path):
    await notes.remember(tmp_path, "х" * 500)
    # Заметки уходят в инструкции при каждом разговоре и стоят токенов.
    assert len(notes.load(tmp_path)[0]) <= 200


async def test_notes_cap_is_enforced(tmp_path):
    for i in range(105):
        await notes.remember(tmp_path, f"факт номер {i}")
    stored = notes.load(tmp_path)
    assert len(stored) <= 100


async def test_instructions_are_empty_without_notes(tmp_path):
    # Пустая строка, а не заголовок без пунктов: иначе модель получает
    # обещание списка, которого нет.
    assert notes.as_instructions(tmp_path) == ""


async def test_instructions_contain_notes(tmp_path):
    await notes.remember(tmp_path, "жену зовут Марина")
    text = notes.as_instructions(tmp_path)
    assert "Марина" in text and text.startswith("\n")


def test_broken_file_does_not_crash(tmp_path):
    (tmp_path / "notes.json").write_text("не json", encoding="utf-8")
    assert notes.load(tmp_path) == []
