"""Второй HTTPS-сервер для сателлита на айфоне (см. main._build_tls_app).

Свой FastAPI-объект без lifespan — важно не грузить модели дважды и не
плодить вторую задачу ночной переиндексации фонотеки. Здесь проверяем
только то, что он действительно раздаёт ту же страницу и тот же сокет,
а не второй набор с независимым состоянием.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.main import _build_tls_app, app, stream


def test_tls_app_has_no_lifespan_side_effects():
    """TLS-приложение не должно объявлять свой lifespan — иначе модели
    (whisper/piper/экран) и ночная переиндексация фонотеки запустятся
    второй раз при поднятии второго uvicorn.Server на том же процессе."""
    tls_app = _build_tls_app()
    assert tls_app.router.lifespan_context is not app.router.lifespan_context


def test_tls_app_serves_satellite_page_with_real_port():
    tls_app = _build_tls_app()
    client = TestClient(tls_app)
    resp = client.get("/satellite")
    assert resp.status_code == 200
    assert "__TLS_PORT__" not in resp.text
    assert f":{settings.tls_port}/satellite" in resp.text


def test_tls_app_reuses_the_same_stream_handler():
    """Оба сервера должны слушать /stream одной и той же функцией — иначе
    комната с колонкой на ws:// и телефон на wss:// окажутся в разных
    сессиях, хотя должны делить один разговор."""
    tls_app = _build_tls_app()
    routes = {r.path: r for r in tls_app.routes if getattr(r, "path", None) == "/stream"}
    assert routes["/stream"].endpoint is stream
