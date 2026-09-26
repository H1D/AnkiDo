# SPDX-License-Identifier: AGPL-3.0-or-later
"""One error shape for the whole service.

Every failure the API reports is an :class:`ApiError` with a stable machine-readable ``code``,
an HTTP status, and a ``retryable`` hint so clients on flaky links know whether to try again.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status: int = 400
    code: str = "bad_request"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            body["details"] = self.details
        return {"error": body}


class Unauthorized(ApiError):
    status = 401
    code = "unauthorized"


class Forbidden(ApiError):
    status = 403
    code = "forbidden"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class Conflict(ApiError):
    status = 409
    code = "conflict"


class PayloadTooLarge(ApiError):
    status = 413
    code = "payload_too_large"


class RateLimited(ApiError):
    status = 429
    code = "rate_limited"
    retryable = True


class SyncRequiredFull(ApiError):
    """The library demands a full sync; we never do that implicitly (invariant 3)."""

    status = 409
    code = "sync_required_full"


class SchemaUpgradeRequired(ApiError):
    """Opening this collection would bump its schema (invariant 4)."""

    status = 409
    code = "schema_upgrade_required"


class ProfileBusy(ApiError):
    status = 503
    code = "profile_busy"
    retryable = True


class ProfileUnavailable(ApiError):
    status = 503
    code = "profile_unavailable"
    retryable = True


class UpstreamError(ApiError):
    """AnkiWeb (or a media host) failed."""

    status = 502
    code = "upstream_error"
    retryable = True


class InternalError(ApiError):
    status = 500
    code = "internal_error"
    retryable = False
