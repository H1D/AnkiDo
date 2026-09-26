# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import dataclasses
import time

import pytest

from ankido.config import Config
from ankido.store import JOURNAL_TTL_SECONDS, TOKEN_PREFIX, Store, TokenRecord, parse_scopes

# ---- tokens ------------------------------------------------------------------------------


def test_create_token_returns_secret_once_and_record(store: Store) -> None:
    raw, rec = store.create_token(
        profile="alice", scopes=frozenset({"read", "review"}), name="kitchen"
    )
    assert raw.startswith(f"{TOKEN_PREFIX}_{rec.id}_")
    assert rec.profile == "alice"
    assert rec.name == "kitchen"
    assert rec.scopes == {"read", "review"}
    assert rec.expires_at is None
    assert rec.revoked_at is None
    assert rec.last_used_at is None
    assert store.get_token(rec.id) == rec
    assert store.get_token("nope") is None


def test_verify_token_round_trip(store: Store) -> None:
    raw, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    got = store.verify_token(raw)
    assert got is not None
    assert got.id == rec.id


def test_verify_rejects_garbage_wrong_secret_and_unknown_id(store: Store) -> None:
    _, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    for bad in (
        "",
        "nope",
        "akd_onlytwo",
        f"xyz_{rec.id}_secret",
        f"akd_{rec.id}_wrongsecret",
        "akd_deadbeef_whatever",
    ):
        assert store.verify_token(bad) is None, bad


def test_list_tokens_all_and_by_profile(store: Store) -> None:
    _, a = store.create_token(profile="alice", scopes=frozenset({"read"}))
    _, b = store.create_token(profile="bob", scopes=frozenset({"read"}))
    _, root = store.create_token(profile=None, scopes=frozenset({"admin"}))
    assert [t.id for t in store.list_tokens()] == [a.id, b.id, root.id]
    assert [t.id for t in store.list_tokens("alice")] == [a.id]
    assert store.list_tokens("nobody") == []


def test_revoke_token(store: Store) -> None:
    raw, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    assert store.revoke_token(rec.id) is True
    assert store.verify_token(raw) is None
    got = store.get_token(rec.id)
    assert got is not None and got.revoked_at is not None
    assert got.is_valid() is False
    assert store.revoke_token(rec.id) is False  # already revoked
    assert store.revoke_token("missing") is False


def test_expired_token_is_invalid(store: Store) -> None:
    raw, rec = store.create_token(
        profile="alice", scopes=frozenset({"read"}), expires_in_seconds=-5
    )
    assert rec.expires_at is not None and rec.expires_at < int(time.time())
    assert rec.is_valid() is False
    assert store.verify_token(raw) is None

    raw2, rec2 = store.create_token(
        profile="alice", scopes=frozenset({"read"}), expires_in_seconds=3600
    )
    assert rec2.is_valid() is True
    assert store.verify_token(raw2) is not None
    assert rec2.expires_at is not None
    assert rec2.is_valid(now=rec2.expires_at) is False
    assert rec2.is_valid(now=rec2.expires_at - 1) is True


def test_touch_token_updates_last_used(store: Store) -> None:
    _, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    store.touch_token(rec.id)
    got = store.get_token(rec.id)
    assert got is not None and got.last_used_at is not None


def test_parse_scopes() -> None:
    assert parse_scopes("read, review") == {"read", "review"}
    assert parse_scopes("admin") == {"admin"}
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("read,delete")
    with pytest.raises(ValueError, match="at least one scope"):
        parse_scopes(" , ")


def _rec(profile: str | None, scopes: set[str], **kw: int | None) -> TokenRecord:
    return TokenRecord(
        id="t1",
        profile=profile,
        name="",
        scopes=frozenset(scopes),
        created_at=int(time.time()),
        expires_at=kw.get("expires_at"),
        revoked_at=kw.get("revoked_at"),
        last_used_at=None,
    )


def test_token_record_allows_profile_bound_vs_global_admin() -> None:
    bound = _rec("alice", {"read"})
    assert bound.allows("alice", "read")
    assert not bound.allows("alice", "add")
    assert not bound.allows("bob", "read")
    assert not bound.is_admin_global

    root = _rec(None, {"admin"})
    assert root.is_admin_global
    assert root.allows("alice", "add")
    assert root.allows("bob", "sync")

    profile_admin = _rec("alice", {"admin"})
    assert not profile_admin.is_admin_global
    assert profile_admin.allows("alice", "sync")
    assert not profile_admin.allows("bob", "read")

    revoked = dataclasses.replace(bound, revoked_at=int(time.time()))
    assert not revoked.allows("alice", "read")
    expired = dataclasses.replace(root, expires_at=int(time.time()) - 1)
    assert not expired.allows("alice", "read")


# ---- journal -----------------------------------------------------------------------------


def test_journal_put_get_get_many_and_replace(store: Store) -> None:
    assert store.journal_get("alice", "review", "r1") is None
    store.journal_put("alice", "review", "r1", {"status": "applied", "card_id": 1})
    store.journal_put("alice", "review", "r2", {"status": "applied", "card_id": 2})
    store.journal_put("alice", "note", "r1", {"status": "added"})  # other kind, same id
    assert store.journal_get("alice", "review", "r1") == {"status": "applied", "card_id": 1}
    assert store.journal_get("bob", "review", "r1") is None
    many = store.journal_get_many("alice", "review", ["r1", "r2", "r3"])
    assert set(many) == {"r1", "r2"}
    assert store.journal_get_many("alice", "review", []) == {}
    store.journal_put("alice", "review", "r1", {"status": "replaced"})
    assert store.journal_get("alice", "review", "r1") == {"status": "replaced"}


def test_journal_get_many_chunks_large_id_lists(store: Store) -> None:
    ids = [f"id-{i}" for i in range(1200)]
    for cid in ids[::3]:
        store.journal_put("alice", "review", cid, {"n": cid})
    got = store.journal_get_many("alice", "review", ids)
    assert len(got) == 400
    assert got["id-3"] == {"n": "id-3"}


def test_journal_prune(store: Store) -> None:
    store.journal_put("alice", "review", "a", {})
    store.journal_put("alice", "review", "b", {})
    assert store.journal_prune(JOURNAL_TTL_SECONDS) == 0
    assert store.journal_prune(ttl_seconds=-5) == 2  # everything older than "now + 5s"
    assert store.journal_get("alice", "review", "a") is None


# ---- audit / meta ------------------------------------------------------------------------


def test_audit_tail_order_filter_and_limit(store: Store) -> None:
    store.audit(token_id="t1", profile="alice", action="notes", outcome="ok", count=3)
    store.audit(token_id="t2", profile="bob", action="sync", outcome="merged")
    store.audit(token_id=None, profile="alice", action="backup", outcome="ok", detail="manual")
    tail = store.audit_tail()
    assert [e["action"] for e in tail] == ["backup", "sync", "notes"]
    assert tail[0]["detail"] == "manual" and tail[0]["token_id"] is None
    assert tail[2]["count"] == 3 and "ts" in tail[2]
    assert [e["action"] for e in store.audit_tail(profile="alice")] == ["backup", "notes"]
    assert [e["action"] for e in store.audit_tail(limit=1)] == ["backup"]
    assert [e["action"] for e in store.audit_tail(limit=1, profile="bob")] == ["sync"]


def test_meta_get_set(store: Store) -> None:
    assert store.meta_get("k") is None
    store.meta_set("k", "v1")
    store.meta_set("k", "v2")
    assert store.meta_get("k") == "v2"


def test_store_persists_across_instances(config: Config, store: Store) -> None:
    raw, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    other = Store(config.state_db_path)
    try:
        got = other.verify_token(raw)
        assert got is not None and got.id == rec.id
    finally:
        other.close()
