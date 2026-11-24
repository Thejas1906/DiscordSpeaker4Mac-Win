"""Structured JSON-lines logging with token redaction."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Optional

from .paths import log_dir

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,7}\.[A-Za-z0-9_\-]{27,}")
_secrets: set = set()


def register_secret(value: Optional[str]) -> None:
    if value:
        _secrets.add(value)


def redact(text: str) -> str:
    for s in _secrets:
        text = text.replace(s, "[REDACTED]")
    return _TOKEN_RE.sub("[REDACTED]", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": round(r.created, 3), "level": r.levelname, "logger": r.name, "msg": redact(r.getMessage())}
        if r.exc_info:
            d["exc"] = redact(self.formatException(r.exc_info))
        return json.dumps(d, ensure_ascii=False)


class SafeFormatter(logging.Formatter):
    """Redacts the *final* text, including formatted tracebacks (exception args often echo secrets)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(component: str, level: int = logging.INFO, stream=sys.stderr) -> Path:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    path = log_dir() / f"{component}.jsonl"
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.addFilter(RedactFilter())
    root.addHandler(fh)
    if stream:
        sh = logging.StreamHandler(stream)
        sh.setFormatter(SafeFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
        sh.addFilter(RedactFilter())
        root.addHandler(sh)
    logging.getLogger("discord").setLevel(logging.WARNING)
    # Voice diagnostics (connect / DAVE E2EE handshake). Debug level is written to the log *file* only.
    logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)
    logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
    logging.getLogger("discord.voice_client").setLevel(logging.INFO)
    return path
