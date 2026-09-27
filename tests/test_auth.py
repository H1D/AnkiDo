# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import time

import pytest

from ankido.auth import Authenticator
from ankido.errors import Forbidden, Unauthorized
from ankido.store import Store, TokenRecord


def test_cache_hit_skips_argon2_verify_and_touches_token(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    auth = Authenticator(store)
    calls = {"verify": 0}
    original = store.verify_token

    def counting(value: str) -> TokenRecord | None:
        calls["verify"] += 1
        return original(value)

    monkeypatch.setattr(store, "verify_token", counting)
    assert auth.authenticate(raw).id == rec.id
    assert auth.authenticate(raw).id == rec.id
    assert calls["verify"] == 1  # second call served from the cache
    got = store.get_token(rec.id)
    assert got is not None and got.last_used_at is not None


def test_revoked_token_dies_immediately_even_when_cached(store: Store) -> None:
    raw, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    auth = Authenticator(store)
    auth.authenticate(raw)
    store.revoke_token(rec.id)
    with pytest.raises(Unauthorized, match="revoked or expired"):
        auth.authenticate(raw)
    # And it stays dead (cache entry dropped, argon2 path also rejects).
    with pytest.raises(Unauthorized):
        auth.authenticate(raw)


def test_expired_token_is_rejected_even_when_cached(store: Store) -> None:
    # 2 s, not 1: expiry is whole seconds, so a 1 s token can lapse before the first check.
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"read"}), expires_in_seconds=2)
    auth = Authenticator(store)
    auth.authenticate(raw)
    time.sleep(2.1)
    with pytest.raises(Unauthorized):
        auth.authenticate(raw)


def test_missing_token_is_unauthorized(store: Store) -> None:
    auth = Authenticator(store)
    for raw in (None, ""):
        with pytest.raises(Unauthorized, match="missing bearer token"):
            auth.authenticate(raw)


def test_invalid_token_is_unauthorized(store: Store) -> None:
    auth = Authenticator(store)
    with pytest.raises(Unauthorized, match="invalid token") as ei:
        auth.authenticate("akd_00000000_" + "x" * 43)
    assert ei.value.status == 401
    assert ei.value.to_dict()["error"]["code"] == "unauthorized"


def test_require_scope_forbidden_with_details(store: Store) -> None:
    _, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    Authenticator.require(rec, "alice", "read")
    with pytest.raises(Forbidden) as ei:
        Authenticator.require(rec, "alice", "add")
    assert ei.value.status == 403
    assert ei.value.details == {"required_scope": "add", "profile": "alice"}
    with pytest.raises(Forbidden):
        Authenticator.require(rec, "bob", "read")


def test_require_admin_and_global_admin(store: Store) -> None:
    _, reader = store.create_token(profile="alice", scopes=frozenset({"read"}))
    _, profile_admin = store.create_token(profile="alice", scopes=frozenset({"admin"}))
    _, root = store.create_token(profile=None, scopes=frozenset({"admin"}))

    with pytest.raises(Forbidden, match="admin scope required"):
        Authenticator.require_admin(reader)
    Authenticator.require_admin(profile_admin)
    Authenticator.require_admin(profile_admin, "alice")
    with pytest.raises(Forbidden, match="bound to another profile"):
        Authenticator.require_admin(profile_admin, "bob")
    Authenticator.require_admin(root, "bob")

    with pytest.raises(Forbidden, match="global admin token required"):
        Authenticator.require_global_admin(profile_admin)
    with pytest.raises(Forbidden):
        Authenticator.require_global_admin(reader)
    Authenticator.require_global_admin(root)

    store.revoke_token(root.id)
    revoked = store.get_token(root.id)
    assert revoked is not None
    with pytest.raises(Forbidden):
        Authenticator.require_admin(revoked)
