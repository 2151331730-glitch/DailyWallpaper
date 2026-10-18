# src/dailywallpaper/logging_setup.py
"""Rotating log with secret redaction."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]+"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"api[_-]?key[\"']?\s*[:=]\s*\S+", re.I),
    re.compile(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
]


def redact(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub("***", text)
    return text


class RedactFilter(logging.Filter):
    """Scrub credentials out of a record before anything writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        record.args = ()
        return True


def setup_logging(data_dir, level: str = "INFO", max_mb: int = 5, backups: int = 5) -> Path:
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=max_mb * 1024 * 1024,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.addFilter(RedactFilter())
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )
    return log_dir / "app.log"
