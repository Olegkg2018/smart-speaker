import pytest

from app.config import settings
from app.protocol import State
from app.screen import ScreenRenderer

pytest.importorskip("PIL")


@pytest.fixture
def renderer() -> ScreenRenderer:
    r = ScreenRenderer(128, 64, settings.screen_font)
    r.load()
    if not r.available:
        pytest.skip("шрифт для экрана недоступен")
    return r


def test_frame_size_matches_ssd1306(renderer):
    frame = renderer.render(State.IDLE, "привет")
    # 128×64 однобитных пикселя — ровно килобайт в постраничной раскладке.
    assert len(frame) == 128 * 64 // 8


def test_header_is_inverted_bar(renderer):
    frame = renderer.render(State.LISTENING)
    # Первая страница — это верхние восемь строк, целиком под шапкой.
    first_page = frame[:128]
    assert all(byte == 0xFF for byte in first_page[100:120])


def test_cyrillic_reaches_the_screen(renderer):
    blank = renderer.render(State.IDLE, "")
    with_text = renderer.render(State.IDLE, "Сейчас двенадцать градусов")
    # Текст должен менять картинку — иначе кириллица молча теряется.
    assert with_text != blank


def test_long_text_is_truncated_not_dropped(renderer):
    long_text = "Слово " * 60
    frame = renderer.render(State.SPEAKING, long_text)
    assert len(frame) == 128 * 64 // 8


def test_now_playing_shown_when_no_text(renderer):
    idle = renderer.render(State.PLAYING, "", None)
    playing = renderer.render(State.PLAYING, "", "Кино — Группа крови")
    assert playing != idle


def test_disabled_renderer_returns_nothing():
    r = ScreenRenderer(128, 64, settings.screen_font)
    # load() не вызывали — колонка должна просто работать без экрана.
    assert r.render(State.IDLE, "текст") == b""
