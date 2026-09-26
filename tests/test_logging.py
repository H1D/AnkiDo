# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import io
import json
import logging

from ankido.logging import (
    JsonFormatter,
    configure_logging,
    get_logger,
    register_secret,
    scrub,
)

SECRET = "correct-horse-battery"
TOKEN = "akd_1a2b3c4d_" + "Q" * 43


def test_scrub_removes_registered_secrets_and_token_shaped_strings() -> None:
    register_secret(SECRET)
    register_secret("ab")  # too short: ignored, so "ab" is not masked everywhere
    register_secret(None)
    out = scrub(f"login {SECRET} token {TOKEN} abc")
    assert SECRET not in out and TOKEN not in out
    assert out == "login *** token akd_*** abc"


def _capture(name: str) -> tuple[logging.Logger, io.StringIO]:
    base = logging.getLogger(name)
    base.handlers.clear()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    base.addHandler(handler)
    base.setLevel(logging.DEBUG)
    base.propagate = False
    return base, stream


def test_json_formatter_emits_json_with_fields_and_scrubs_values() -> None:
    register_secret(SECRET)
    _, stream = _capture("ankido.test.fields")
    log = get_logger("ankido.test.fields")
    log.info(
        "hello %s",
        "world",
        fields={
            "token": TOKEN,
            "nested": {"pw": SECRET, "n": 1},
            "list": [SECRET, 2],
            "path": "/v1/p/alice/queue",
        },
    )
    data = json.loads(stream.getvalue())
    assert data["msg"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "ankido.test.fields"
    assert data["ts"].endswith("Z") and "T" in data["ts"]
    assert data["token"] == "akd_***"
    assert data["nested"] == {"pw": "***", "n": 1}
    assert data["list"] == ["***", 2]
    assert data["path"] == "/v1/p/alice/queue"


def test_json_formatter_scrubs_exception_text() -> None:
    register_secret(SECRET)
    _, stream = _capture("ankido.test.exc")
    log = get_logger("ankido.test.exc")
    try:
        raise RuntimeError(f"bad password {SECRET}")
    except RuntimeError:
        log.exception("boom")
    data = json.loads(stream.getvalue())
    assert data["level"] == "ERROR"
    assert "RuntimeError" in data["exc"]
    assert SECRET not in data["exc"] and "***" in data["exc"]


def test_configure_logging_installs_json_handler() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging("debug")
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
