# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured JSON logging with secret scrubbing.

Anything registered with :func:`register_secret` (AnkiWeb passwords, raw tokens) is replaced in
every log line, including exception text. Token-shaped strings are scrubbed by pattern regardless.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from typing import Any

TOKEN_RE = re.compile(r"akd_[A-Za-z0-9]{6,}_[A-Za-z0-9_-]{16,}")

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        with _secrets_lock:
            _secrets.add(value)


def scrub(text: str) -> str:
    text = TOKEN_RE.sub("akd_***", text)
    with _secrets_lock:
        for s in _secrets:
            if s in text:
                text = text.replace(s, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():  # type: ignore[union-attr]
                payload[str(k)] = _scrub_value(v)
        if record.exc_info and record.exc_info[1] is not None:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def _scrub_value(v: Any) -> Any:
    if isinstance(v, str):
        return scrub(v)
    if isinstance(v, dict):
        return {str(k): _scrub_value(x) for k, x in v.items()}  # type: ignore[union-attr]
    if isinstance(v, list | tuple):
        return [_scrub_value(x) for x in v]  # type: ignore[union-attr]
    return v


class _ExtraAdapter(logging.LoggerAdapter[logging.Logger]):
    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        fields = kwargs.pop("fields", None)
        extra = kwargs.get("extra") or {}
        if fields:
            extra = {**extra, "extra_fields": fields}
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str) -> _ExtraAdapter:
    return _ExtraAdapter(logging.getLogger(name), {})


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel("WARNING")
