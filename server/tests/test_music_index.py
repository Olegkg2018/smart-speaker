"""Индекс фонотеки: разметка новых треков, пропуск старых, поиск по тегам."""

import json

import httpx
import pytest

from app.audio import music_index


def _fake_openai(handler):
    original = httpx.AsyncClient

    class Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    httpx.AsyncClient = Patched
    return original


def _tagged_response(tags: dict[int, str]) -> httpx.Response:
    content = json.dumps([{"i": i, "tags": t} for i, t in tags.items()])
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def library(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "Queen - Dont Stop Me Now.mp3").write_bytes(b"")
    (music_dir / "Rain Sounds.mp3").write_bytes(b"")
    return music_dir


async def test_refresh_classifies_new_tracks(library, tmp_path):
    index_dir = tmp_path / "index"

    def handler(request):
        return _tagged_response({0: "диско весёлая для вечеринки", 1: "шум дождя для сна"})

    original = _fake_openai(handler)
    try:
        classified = await music_index.refresh(library, index_dir, "sk-test", "gpt-4o-mini")
    finally:
        httpx.AsyncClient = original

    assert classified == 2
    data = json.loads(music_index.index_file_path(index_dir).read_text(encoding="utf-8"))
    assert len(data["tracks"]) == 2


async def test_refresh_skips_unchanged_tracks(library, tmp_path):
    index_dir = tmp_path / "index"
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return _tagged_response({0: "диско весёлая", 1: "шум дождя"})

    original = _fake_openai(handler)
    try:
        await music_index.refresh(library, index_dir, "sk-test", "gpt-4o-mini")
        # Фонотека не менялась — второй прогон не должен звать модель снова.
        second = await music_index.refresh(library, index_dir, "sk-test", "gpt-4o-mini")
    finally:
        httpx.AsyncClient = original

    assert second == 0
    assert calls == 1


async def test_refresh_forgets_deleted_files(library, tmp_path):
    index_dir = tmp_path / "index"

    def handler(request):
        return _tagged_response({0: "диско", 1: "шум дождя"})

    original = _fake_openai(handler)
    try:
        await music_index.refresh(library, index_dir, "sk-test", "gpt-4o-mini")
    finally:
        httpx.AsyncClient = original

    (library / "Rain Sounds.mp3").unlink()

    def handler_no_calls(request):
        raise AssertionError("не должно быть новых запросов — только удаление")

    original = _fake_openai(handler_no_calls)
    try:
        await music_index.refresh(library, index_dir, "sk-test", "gpt-4o-mini")
    finally:
        httpx.AsyncClient = original

    data = json.loads(music_index.index_file_path(index_dir).read_text(encoding="utf-8"))
    assert len(data["tracks"]) == 1


async def test_refresh_without_api_key_saves_but_does_not_classify(library, tmp_path):
    index_dir = tmp_path / "index"
    classified = await music_index.refresh(library, index_dir, "", "gpt-4o-mini")
    assert classified == 0
    data = json.loads(music_index.index_file_path(index_dir).read_text(encoding="utf-8"))
    assert data["tracks"] == {}


def test_search_matches_by_mood_tag(library, tmp_path):
    index_dir = tmp_path / "index"
    music_index._save(
        music_index.index_file_path(index_dir),
        {
            "Queen - Dont Stop Me Now.mp3": music_index.TrackTags(
                title="Don't Stop Me Now", artist="Queen", tags="диско весёлая для вечеринки", mtime=0.0
            ),
            "Rain Sounds.mp3": music_index.TrackTags(
                title="Rain Sounds", artist="", tags="шум дождя для сна", mtime=0.0
            ),
        },
    )

    found = music_index.search(library, index_dir, "поставь весёлую музыку")
    assert found is not None and "Stop Me Now" in found[1]

    found_rain = music_index.search(library, index_dir, "включи шум дождя")
    assert found_rain is not None and "Rain" in found_rain[1]


def test_search_without_index_returns_none(tmp_path):
    assert music_index.search(tmp_path, tmp_path / "no-index", "весёлая музыка") is None
