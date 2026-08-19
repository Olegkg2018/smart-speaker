import pytest

from app.audio.player import _search_local


@pytest.fixture
def library(tmp_path):
    tracks = [
        "rock/Легенды русского рока/33. Пикник - У шамана три руки.mp3",
        "rock/Легенды русского рока/50. Виктор Цой - Группа крови.mp3",
        "jazz/Miles Davis - So What.mp3",
    ]
    for track in tracks:
        path = tmp_path / track
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return tmp_path


def test_partial_artist_match_does_not_hijack_other_song(library):
    # Совпадает только «пикник» — это другая песня, пусть ищет в интернете.
    assert _search_local("Пикник Иероглиф", library) is None


def test_finds_track_when_most_words_match(library):
    # «кино» в имени файла нет, но «группа» и «крови» есть.
    found = _search_local("Кино Группа крови", library)
    assert found is not None and "Группа крови" in found.name


def test_single_word_query_matches_artist(library):
    found = _search_local("Пикник", library)
    assert found is not None and "Пикник" in found.name


def test_unknown_query_returns_nothing(library):
    assert _search_local("Rammstein Du Hast", library) is None


def test_short_words_are_ignored(library):
    assert _search_local("а и в", library) is None


def test_missing_library_is_not_an_error(tmp_path):
    assert _search_local("что угодно", tmp_path / "нет-такой-папки") is None
