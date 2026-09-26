# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request-scoped helpers: auth, scope checks, rate limiting, audit."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ankido.errors import Unauthorized
from ankido.store import TokenRecord
from ankido.supervisor import Supervisor
from ankido.worker import ProfileWorker


def supervisor_of(request: Request) -> Supervisor:
    return request.app.state.supervisor


def bearer_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


@dataclass
class Principal:
    token: TokenRecord
    worker: ProfileWorker
    profile: str


def authorize(request: Request, profile: str, scope: str, *, klass: str) -> Principal:
    """Authenticate, check scope on ``profile``, apply rate limits. ``klass`` selects the bucket."""
    sup = supervisor_of(request)
    raw = bearer_from(request)
    if raw is None:
        raise Unauthorized("missing bearer token")
    token = sup.auth.authenticate(raw)
    # Only authenticated callers learn whether a profile exists.
    worker = sup.worker(profile)
    sup.auth.require(token, profile, scope)
    sup.limiter.check(token_id=token.id, profile=profile, klass=klass)
    request.state.token_id = token.id
    return Principal(token=token, worker=worker, profile=profile)


def authorize_admin(request: Request, profile: str | None = None) -> TokenRecord:
    sup = supervisor_of(request)
    raw = bearer_from(request)
    token = sup.auth.authenticate(raw)
    sup.auth.require_admin(token, profile)
    request.state.token_id = token.id
    return token
