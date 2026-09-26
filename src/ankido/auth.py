# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bearer-token authentication and scope checks.

argon2 verification is deliberately slow (~50 ms), so successful verifications are cached in
memory for a short time keyed by a SHA-256 of the raw token. Revocation is re-checked on every
request against the store (a cheap primary-key lookup), so a revoked token dies immediately.
"""

from __future__ import annotations

import hashlib
import threading
import time

from ankido.errors import Forbidden, Unauthorized
from ankido.store import Store, TokenRecord

_CACHE_TTL = 300.0


class Authenticator:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._cache: dict[str, tuple[float, str]] = {}  # sha256(raw) -> (expiry, token_id)
        self._lock = threading.Lock()

    def authenticate(self, raw: str | None, *, legacy: bool = False) -> TokenRecord:
        if not raw:
            raise Unauthorized("missing bearer token")
        key = hashlib.sha256(raw.encode()).hexdigest()
        now = time.monotonic()
        token_id: str | None = None
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                token_id = hit[1]
        rec: TokenRecord | None
        if token_id is not None:
            rec = self._store.get_token(token_id)
            if rec is None or not rec.is_valid():
                with self._lock:
                    self._cache.pop(key, None)
                raise Unauthorized("token revoked or expired")
        else:
            rec = self._store.verify_token(raw)
            if rec is None:
                raise Unauthorized("invalid token")
            with self._lock:
                self._cache[key] = (now + _CACHE_TTL, rec.id)
        self._store.touch_token(rec.id)
        return rec

    @staticmethod
    def require(rec: TokenRecord, profile: str, scope: str) -> None:
        if not rec.allows(profile, scope):
            raise Forbidden(
                f"token lacks scope {scope!r} on profile {profile!r}",
                details={"required_scope": scope, "profile": profile},
            )

    @staticmethod
    def require_admin(rec: TokenRecord, profile: str | None = None) -> None:
        if not rec.is_valid() or "admin" not in rec.scopes:
            raise Forbidden("admin scope required")
        if profile is not None and rec.profile is not None and rec.profile != profile:
            raise Forbidden("token is bound to another profile")

    @staticmethod
    def require_global_admin(rec: TokenRecord) -> None:
        if not rec.is_valid() or "admin" not in rec.scopes or rec.profile is not None:
            raise Forbidden("global admin token required")
