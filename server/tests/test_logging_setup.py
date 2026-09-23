"""Лог должен идти и в docker logs, и в файл, переживающий пересоздание
контейнера (см. CLAUDE.md — docker logs стирается на каждый
--force-recreate, а мы его делаем часто во время активной доработки).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import app.main as main_mod


def test_root_logger_has_both_stream_and_file_handlers():
    handlers = logging.getLogger().handlers

    assert any(
        type(h) is logging.StreamHandler for h in handlers
    ), "docker logs не должен был пропасть — это первая линия диагностики"

    file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert file_handlers, "лог обязан писаться в файл, а не только в docker logs"
    handler = file_handlers[0]
    assert handler.maxBytes == main_mod._LOG_MAX_BYTES
    assert handler.backupCount == main_mod._LOG_BACKUP_COUNT
