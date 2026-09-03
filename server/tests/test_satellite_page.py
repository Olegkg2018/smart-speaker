"""Страница /satellite: рендер и ссылка на страницу управления.

Общий вид (webstyle.BASE_CSS/nav) подставляется через .replace() наравне с
__TLS_PORT__ — здесь проверяется, что все три плейсхолдера действительно
заменяются, а не остаются в HTML как текст.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import satellite_page


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(satellite_page.router)
    return TestClient(app)


def test_page_renders_with_no_leftover_placeholders():
    html = _client().get("/satellite").text
    assert "__TLS_PORT__" not in html
    assert "__BASE_CSS__" not in html
    assert "__NAV__" not in html


def test_page_links_back_to_management_page():
    html = _client().get("/satellite").text
    assert '<a class="here" href="/satellite">' in html
    assert 'href="/"' in html


def test_page_has_a_volume_control_for_the_speaker():
    html = _client().get("/satellite").text
    assert 'id="volSlider"' in html
    assert "t: 'volume'" in html or "t:'volume'" in html

