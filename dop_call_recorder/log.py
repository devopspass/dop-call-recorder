"""Structured JSON-lines logging (stderr + optional file)."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        d.update({k: v for k, v in record.__dict__.items() if k not in _STD})
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def setup(level: str = "INFO", file: str | None = None, max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> logging.Logger:
    root = logging.getLogger("dop_call_recorder")
    root.handlers.clear()
    root.setLevel(level.upper())
    root.propagate = False
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file:
        p = Path(file).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(p, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"))
        except OSError:
            pass
    for h in handlers:
        h.setFormatter(JsonFormatter())
        root.addHandler(h)
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"dop_call_recorder.{name}")


def event(logger: logging.Logger, msg: str, **fields) -> None:
    logger.info(msg, extra=fields)
