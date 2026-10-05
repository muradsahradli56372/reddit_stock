"""Structured logging. LOG_FORMAT=json emits one JSON object per line (for log shippers);
the default text format appends structured fields as key=value.

Use: log.info("week processed", extra={"week_start": ..., "posts": ...})
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def _extras(record: logging.LogRecord) -> dict:
    return {k: v for k, v in vars(record).items() if k not in _STD and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _extras(record)
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def setup_logging(fmt: str = "text", level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "prawcore", "urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
