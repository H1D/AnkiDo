# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/v1`` through the FastAPI TestClient (lifespan on, real collections in tmp_path)."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankido.api.app import create_app
from ankido.collection import ops
from ankido.collection.session import Session, SyncOutcome, SyncResult
from ankido.config import Credentials
from ankido.errors import SyncRequiredFull
from ankido.store import Store
from conftest import ConfigMaker, Seeder, auth

NOTES_BODY: dict[str, Any] = {
    "deck": "Dutch::Common",
    "model": "Basic",
    "notes": [{"fields": {"Front": "het huis", "Back": "house"}}],
}
REVIEWS_BODY: dict[str, Any] = {"reviews": [{"card_id": 1, "ease": 3}]}


def assert_error(r: httpx2.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert err["code"] == code
    assert isinstance(err["message"], str) and isinstance(err["retryable"], bool)
    return err


@pytest.fixture
def patched_sync(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Replace Session.sync with a fake that never touches the network."""
    calls: list[str | None] = []

    def sync(self: Session, *, force_full: str | None = None) -> SyncResult:
        calls.append(force_full)
        outcome = SyncOutcome.UPLOADED if force_full == "upload" else SyncOutcome.MERGED
        return SyncResult(
            outcome=outcome,
            local_replaced=False,
            server_message="ok",
            media="synced",
            duration_ms=5,
        )

    monkeypatch.setattr(Session, "sync", sync)
    return calls


# ---- auth surface -------------------------------------------------------------------------


def test_healthz_needs_no_auth_and_carries_no_data(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


V1_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/v1/p/alice/notes", NOTES_BODY),
    ("post", "/v1/p/alice/reviews", REVIEWS_BODY),
    ("get", "/v1/p/alice/queue", None),
    ("post", "/v1/p/alice/exchange", {}),
    ("get", "/v1/p/alice/stats", None),
    ("get", "/v1/p/alice/decks", None),
    ("get", "/v1/p/alice/media/x.mp3", None),
    ("post", "/v1/p/alice/sync", None),
    ("get", "/v1/p/alice/sync/status", None),
    ("post", "/v1/p/alice/backup", None),
    ("get", "/v1/admin/profiles", None),
    ("get", "/v1/admin/metrics", None),
    ("get", "/v1/admin/audit", None),
]


@pytest.mark.parametrize(("method", "path", "body"), V1_ENDPOINTS)
def test_every_v1_endpoint_is_401_without_a_valid_token(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    r = client.request(method, path, json=body)
    assert_error(r, 401, "unauthorized")
    assert r.headers["www-authenticate"] == "Bearer"
    r = client.request(method, path, json=body, headers={"Authorization": "Basic abc"})
    assert_error(r, 401, "unauthorized")
    r = client.request(method, path, json=body, headers=auth("akd_00000000_" + "x" * 43))
    assert_error(r, 401, "unauthorized")


WRONG_SCOPE: list[tuple[str, str, dict[str, Any] | None, str | None]] = [
    ("post", "/v1/p/alice/notes", NOTES_BODY, "add"),
    ("post", "/v1/p/alice/reviews", REVIEWS_BODY, "review"),
    ("post", "/v1/p/alice/exchange", REVIEWS_BODY, "review"),
    ("post", "/v1/p/alice/sync", None, "sync"),
    ("post", "/v1/p/alice/backup", None, None),
    ("get", "/v1/admin/profiles", None, None),
    ("get", "/v1/admin/metrics", None, None),
    ("get", "/v1/admin/audit", None, None),
]


@pytest.mark.parametrize(("method", "path", "body", "scope"), WRONG_SCOPE)
def test_wrong_scope_is_403(
    client: TestClient,
    alice_read_token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    scope: str | None,
) -> None:
    r = client.request(method, path, json=body, headers=auth(alice_read_token))
    err = assert_error(r, 403, "forbidden")
    if scope:
        assert err["details"] == {"required_scope": scope, "profile": "alice"}


@pytest.mark.parametrize(
    "path",
    ["/v1/p/alice/queue", "/v1/p/alice/stats", "/v1/p/alice/decks", "/v1/p/alice/sync/status"],
)
def test_other_profiles_token_is_403(client: TestClient, bob_token: str, path: str) -> None:
    assert_error(client.get(path, headers=auth(bob_token)), 403, "forbidden")


def test_unknown_profile_is_404(client: TestClient, alice_token: str, admin_token: str) -> None:
    # Only authenticated callers learn whether a profile exists.
    assert_error(client.get("/v1/p/zed/queue"), 401, "unauthorized")
    for token in (alice_token, admin_token):
        assert_error(client.get("/v1/p/zed/queue", headers=auth(token)), 404, "profile_not_found")
    assert_error(
        client.post("/v1/p/zed/backup", headers=auth(admin_token)), 404, "profile_not_found"
    )
    r = client.post(
        "/v1/p/zed/sync", json={"force_full": "upload", "confirm": "zed"}, headers=auth(admin_token)
    )
    assert_error(r, 404, "profile_not_found")


def test_validation_error_has_the_same_shape(client: TestClient, alice_token: str) -> None:
    r = client.post("/v1/p/alice/notes", json={}, headers=auth(alice_token))
    err = assert_error(r, 422, "validation_error")
    assert err["retryable"] is False
    errors = err["details"]["errors"]
    assert errors and set(errors[0]) == {"loc", "msg", "type"}
    bad_note = {**NOTES_BODY, "notes": [{"fields": {"Front": "a"}, "bogus": 1}]}
    assert_error(
        client.post("/v1/p/alice/notes", json=bad_note, headers=auth(alice_token)),
        422,
        "validation_error",
    )
    for query in ("limit=0", "fields=weird", "render=pdf", "max_new_per_day=-1"):
        r = client.get(f"/v1/p/alice/queue?{query}", headers=auth(alice_token))
        assert_error(r, 422, "validation_error")
    r = client.post(
        "/v1/p/alice/reviews",
        json={"reviews": [{"card_id": 1, "ease": 5}]},
        headers=auth(alice_token),
    )
    assert_error(r, 422, "validation_error")


# ---- notes / media ------------------------------------------------------------------------


def test_notes_add_replay_and_dedupe_modes(client: TestClient, alice_token: str) -> None:
    body = {
        "deck": "Dutch::Common",
        "model": "Basic (and reversed card)",
        "tags": ["src:reader"],
        "dedupe": "skip",
        "notes": [
            {
                "client_id": "reader-2026-09-26-0001",
                "fields": {"Front": "het huis", "Back": "house"},
            }
        ],
    }
    r1 = client.post("/v1/p/alice/notes", json=body, headers=auth(alice_token))
    assert r1.status_code == 200, r1.text
    res = r1.json()["results"][0]
    assert res["status"] == "added" and res["client_id"] == "reader-2026-09-26-0001"
    assert len(res["card_ids"]) == 2 and res["media"] == []

    r2 = client.post("/v1/p/alice/notes", json=body, headers=auth(alice_token))
    replay = r2.json()["results"][0]
    assert replay["status"] == "added" and replay["replayed"] is True
    assert replay["note_id"] == res["note_id"]

    plain = {**body, "notes": [{"fields": {"Front": "HET HUIS", "Back": "home"}}]}
    r3 = client.post("/v1/p/alice/notes", json=plain, headers=auth(alice_token))
    assert r3.json()["results"][0]["status"] == "skipped_duplicate"
    r4 = client.post(
        "/v1/p/alice/notes", json={**plain, "dedupe": "update"}, headers=auth(alice_token)
    )
    assert r4.json()["results"][0]["status"] == "updated"
    r5 = client.post(
        "/v1/p/alice/notes", json={**plain, "dedupe": "allow"}, headers=auth(alice_token)
    )
    assert r5.json()["results"][0]["status"] == "added"
    decks = client.get("/v1/p/alice/decks", headers=auth(alice_token)).json()["decks"]
    assert {d["name"] for d in decks} == {"Dutch", "Dutch::Common"}


def test_notes_with_audio_then_media_endpoint_range_and_traversal(
    client: TestClient, alice_token: str
) -> None:
    payload = b"ID3synthetic-audio-bytes-0123456789"
    body = {
        **NOTES_BODY,
        "notes": [
            {
                "fields": {"Front": "het huis", "Back": "house"},
                "audio": [
                    {
                        "filename": "huis.mp3",
                        "data": base64.b64encode(payload).decode(),
                        "fields": ["Front"],
                    }
                ],
            }
        ],
    }
    r = client.post("/v1/p/alice/notes", json=body, headers=auth(alice_token))
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["media"] == ["huis.mp3"]

    r = client.get("/v1/p/alice/media/huis.mp3", headers=auth(alice_token))
    assert r.status_code == 200 and r.content == payload
    assert r.headers["content-type"] == "audio/mpeg"

    r = client.get(
        "/v1/p/alice/media/huis.mp3", headers={**auth(alice_token), "Range": "bytes=0-3"}
    )
    assert r.status_code == 206 and r.content == payload[:4]
    assert r.headers["content-range"].startswith("bytes 0-3/")

    assert_error(
        client.get("/v1/p/alice/media/nope.mp3", headers=auth(alice_token)), 404, "media_not_found"
    )
    assert_error(
        client.get("/v1/p/alice/media/a%5Cb.mp3", headers=auth(alice_token)), 404, "media_not_found"
    )
    for traversal in ("..%2Fcollection.anki2", "..%2F..%2Fankido.db", "%2E%2E"):
        r = client.get(f"/v1/p/alice/media/{traversal}", headers=auth(alice_token))
        assert r.status_code == 404, traversal
    # the queue lists the stored file as media to fetch
    card = client.get("/v1/p/alice/queue", headers=auth(alice_token)).json()["cards"][0]
    assert card["media"] == ["huis.mp3"] and card["q"] == "het huis"


# ---- queue / reviews / exchange -----------------------------------------------------------


def test_queue_compact_cursor_and_etag_304(
    client: TestClient, alice_token: str, seed_notes: Seeder
) -> None:
    seed_notes(3)
    r = client.get(
        "/v1/p/alice/queue?limit=2&kinds=new,learning&decks=Test", headers=auth(alice_token)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["cards"]) == 2 and body["counts"]["new"] == 3
    assert body["decks"] == ["Test"] and body["next_cursor"]
    assert set(body["cards"][0]) >= {"card_id", "q", "a", "media", "deck_rank", "kind", "next"}
    etag = r.headers["etag"]
    assert r.headers["cache-control"] == "private, max-age=0"

    r304 = client.get(
        "/v1/p/alice/queue?limit=2&kinds=new,learning&decks=Test",
        headers={**auth(alice_token), "If-None-Match": f'W/"other", {etag}'},
    )
    assert r304.status_code == 304 and r304.content == b""
    assert r304.headers["etag"] == etag

    page2 = client.get(
        f"/v1/p/alice/queue?limit=2&kinds=new,learning&decks=Test&cursor={body['next_cursor']}",
        headers=auth(alice_token),
    ).json()
    assert len(page2["cards"]) == 1 and page2["next_cursor"] is None
    ids = {c["card_id"] for c in body["cards"]} | {c["card_id"] for c in page2["cards"]}
    assert len(ids) == 3

    seed_notes(1)
    r2 = client.get(
        "/v1/p/alice/queue?limit=2&kinds=new,learning&decks=Test",
        headers={**auth(alice_token), "If-None-Match": etag},
    )
    assert r2.status_code == 200 and r2.headers["etag"] != etag

    full = client.get("/v1/p/alice/queue?fields=full&render=html", headers=auth(alice_token))
    assert "question_html" in full.json()["cards"][0]

    assert_error(
        client.get("/v1/p/alice/queue?kinds=bogus", headers=auth(alice_token)), 400, "invalid_kinds"
    )
    assert_error(
        client.get("/v1/p/alice/queue?decks=Nope", headers=auth(alice_token)), 404, "deck_not_found"
    )
    assert_error(
        client.get("/v1/p/alice/queue?cursor=garbage", headers=auth(alice_token)),
        400,
        "invalid_cursor",
    )


def test_reviews_apply_and_replay_is_duplicate(
    client: TestClient, alice_token: str, seed_notes: Seeder, store: Store
) -> None:
    cid = seed_notes(1)[0]["card_ids"][0]
    body = {
        "reviews": [
            {
                "client_id": "dev7f3a-0012",
                "card_id": cid,
                "ease": 3,
                "answered_at": time.time() - 5,
                "time_ms": 4200,
            }
        ]
    }
    r1 = client.post("/v1/p/alice/reviews", json=body, headers=auth(alice_token))
    assert r1.status_code == 200, r1.text
    first = r1.json()["results"][0]
    assert first["status"] == "applied" and first["card_id"] == cid
    assert {"interval_days", "due", "queue", "type", "answered_at"} <= set(first)

    second = client.post("/v1/p/alice/reviews", json=body, headers=auth(alice_token)).json()
    assert second["results"][0]["status"] == "duplicate"
    assert second["results"][0]["interval_days"] == first["interval_days"]

    r = client.post("/v1/p/alice/reviews", json=REVIEWS_BODY, headers=auth(alice_token))
    assert r.json()["results"][0] == {
        "status": "rejected",
        "reason": "card_not_found",
        "card_id": 1,
    }
    tail = store.audit_tail(10, profile="alice")
    assert [e["action"] for e in tail[:3]] == ["reviews", "reviews", "reviews"]
    assert tail[2]["count"] == 1 and tail[1]["count"] == 0


def test_exchange_applies_reviews_and_returns_next_batch(
    client: TestClient, alice_token: str, seed_notes: Seeder
) -> None:
    cid = seed_notes(3)[0]["card_ids"][0]
    body = {
        "reviews": [{"client_id": "x-1", "card_id": cid, "ease": 3}],
        "want": {"decks": ["Test"], "kinds": ["new"], "limit": 60},
    }
    r = client.post("/v1/p/alice/exchange", json=body, headers=auth(alice_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["reviews"][0]["status"] == "applied"
    assert out["sync"] is None  # autosync off, no credentials
    assert len(out["cards"]) == 2 and out["counts"]["returned"] == 2
    assert out["decks"] == ["Test"] and out["next_cursor"] is None
    empty = client.post("/v1/p/alice/exchange", json={}, headers=auth(alice_token)).json()
    assert empty["reviews"] == [] and len(empty["cards"]) == 3


def test_exchange_without_reviews_needs_only_read_scope(
    client: TestClient, alice_read_token: str, seed_notes: Seeder
) -> None:
    seed_notes(1)
    r = client.post(
        "/v1/p/alice/exchange", json={"want": {"limit": 5}}, headers=auth(alice_read_token)
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["cards"]) == 1 and r.json()["reviews"] == []


def test_exchange_runs_sync_when_configured(
    client: TestClient,
    app: FastAPI,
    alice_token: str,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = app.state.supervisor.workers["alice"]
    worker.session.credentials = Credentials(username="u@example.com", password="pw")
    worker.cfg.autosync = "nightly"  # anything but "off"; nightly never fires during the test
    calls: list[str | None] = []

    def sync(self: Session, *, force_full: str | None = None) -> SyncResult:
        calls.append(force_full)
        if len(calls) == 2:
            raise SyncRequiredFull("full sync needed")
        return SyncResult(
            outcome=SyncOutcome.NO_CHANGES,
            local_replaced=False,
            server_message="",
            media="synced",
            duration_ms=1,
        )

    monkeypatch.setattr(Session, "sync", sync)
    out = client.post("/v1/p/alice/exchange", json={}, headers=auth(alice_token)).json()
    assert out["sync"]["outcome"] == "no_changes"
    out = client.post("/v1/p/alice/exchange", json={}, headers=auth(alice_token)).json()
    assert out["sync"]["error"]["code"] == "sync_required_full"
    out = client.post(
        "/v1/p/alice/exchange", json={"sync": "never"}, headers=auth(alice_token)
    ).json()
    assert out["sync"] is None
    # the inline sync does not need the `sync` scope: a read,review device token gets it too
    device, _ = store.create_token(profile="alice", scopes=frozenset({"read", "review"}))
    out = client.post("/v1/p/alice/exchange", json={}, headers=auth(device)).json()
    assert out["sync"]["outcome"] == "no_changes"
    assert calls == [None, None, None]


# ---- stats / decks / sync status ----------------------------------------------------------


def test_stats_and_decks_with_etag(
    client: TestClient, alice_token: str, seed_notes: Seeder
) -> None:
    seed_notes(2, deck="Lang::Dutch")
    r = client.get("/v1/p/alice/stats?days=30", headers=auth(alice_token))
    assert r.status_code == 200, r.text
    stats = r.json()
    assert {
        "reviewed_by_day",
        "reviewed_today",
        "decks",
        "day_rollover_hour",
        "next_day_at",
    } <= set(stats)
    assert (
        client.get(
            "/v1/p/alice/stats?days=30",
            headers={**auth(alice_token), "If-None-Match": r.headers["etag"]},
        ).status_code
        == 304
    )
    assert_error(
        client.get("/v1/p/alice/stats?days=0", headers=auth(alice_token)), 422, "validation_error"
    )

    r = client.get("/v1/p/alice/decks", headers=auth(alice_token))
    decks = {d["name"]: d for d in r.json()["decks"]}
    assert decks["Lang::Dutch"]["parent"] == "Lang" and decks["Lang::Dutch"]["new"] == 2
    assert (
        client.get(
            "/v1/p/alice/decks", headers={**auth(alice_token), "If-None-Match": r.headers["etag"]}
        ).status_code
        == 304
    )


def test_sync_status_without_credentials(client: TestClient, alice_token: str) -> None:
    r = client.get("/v1/p/alice/sync/status", headers=auth(alice_token))
    assert r.status_code == 200
    assert r.json() == {
        "last_sync_at": None,
        "last_result": None,
        "last_error": None,
        "full_sync_pending": False,
        "configured": False,
    }


# ---- sync ----------------------------------------------------------------------------------


def test_sync_without_credentials_is_sync_not_configured(
    client: TestClient, alice_token: str, store: Store
) -> None:
    r = client.post("/v1/p/alice/sync", headers=auth(alice_token))
    err = assert_error(r, 400, "sync_not_configured")
    assert err["retryable"] is False
    tail = store.audit_tail(1, profile="alice")[0]
    assert tail["action"] == "sync" and tail["outcome"] == "error:sync_not_configured"


def test_sync_happy_path_and_nowait(
    client: TestClient, alice_token: str, store: Store, patched_sync: list[str | None]
) -> None:
    r = client.post("/v1/p/alice/sync", json={}, headers=auth(alice_token))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "outcome": "merged",
        "local_replaced": False,
        "server_message": "ok",
        "media": "synced",
        "duration_ms": 5,
    }
    assert store.audit_tail(1, profile="alice")[0]["outcome"] == "merged"
    r = client.post("/v1/p/alice/sync", json={"wait": False}, headers=auth(alice_token))
    assert r.json() == {"outcome": "queued"}
    assert store.audit_tail(1, profile="alice")[0]["outcome"] == "queued"
    deadline = time.monotonic() + 3
    while len(patched_sync) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert patched_sync == [None, None]


def test_force_full_requires_admin_and_confirm(
    client: TestClient, alice_token: str, admin_token: str, alice_admin_token: str
) -> None:
    body = {"force_full": "upload", "confirm": "alice"}
    assert_error(
        client.post("/v1/p/alice/sync", json=body, headers=auth(alice_token)), 403, "forbidden"
    )
    assert_error(
        client.post("/v1/p/alice/sync", json={"force_full": "upload"}, headers=auth(admin_token)),
        400,
        "confirmation_required",
    )
    assert_error(
        client.post(
            "/v1/p/alice/sync",
            json={"force_full": "upload", "confirm": "bob"},
            headers=auth(admin_token),
        ),
        400,
        "confirmation_required",
    )
    # a profile-bound admin token cannot force-sync another profile
    assert_error(
        client.post(
            "/v1/p/bob/sync",
            json={"force_full": "upload", "confirm": "bob"},
            headers=auth(alice_admin_token),
        ),
        403,
        "forbidden",
    )
    assert_error(
        client.post("/v1/p/alice/sync", json={"force_full": "sideways"}, headers=auth(admin_token)),
        422,
        "validation_error",
    )


def test_force_full_with_admin_and_confirm_is_audited(
    client: TestClient, admin_token: str, store: Store, patched_sync: list[str | None]
) -> None:
    r = client.post(
        "/v1/p/alice/sync",
        json={"force_full": "upload", "confirm": "alice"},
        headers=auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "uploaded"
    assert patched_sync == ["upload"]
    tail = store.audit_tail(2, profile="alice")
    assert [(e["action"], e["outcome"]) for e in reversed(tail)] == [
        ("force_full_upload", "requested"),
        ("force_full_upload", "uploaded"),
    ]


def test_force_full_error_is_audited(
    client: TestClient, admin_token: str, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sync(self: Session, *, force_full: str | None = None) -> SyncResult:
        raise SyncRequiredFull("nope", details={"required": "full"})

    monkeypatch.setattr(Session, "sync", sync)
    r = client.post(
        "/v1/p/alice/sync",
        json={"force_full": "download", "confirm": "alice"},
        headers=auth(admin_token),
    )
    err = assert_error(r, 409, "sync_required_full")
    assert err["details"] == {"required": "full"}
    assert store.audit_tail(1, profile="alice")[0]["outcome"] == "error:sync_required_full"


# ---- admin ---------------------------------------------------------------------------------


def test_admin_profiles(
    client: TestClient, admin_token: str, alice_admin_token: str, alice_token: str
) -> None:
    client.get("/v1/p/alice/decks", headers=auth(alice_token))  # opens alice
    r = client.get("/v1/admin/profiles", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    profiles = {p["profile"]: p for p in r.json()["profiles"]}
    assert set(profiles) == {"alice", "bob"}
    alice = profiles["alice"]
    assert alice["open"] is True and alice["schema_version"] == 18
    assert alice["queue_depth"] == 0 and alice["syncing"] is False and alice["current_op"] is None
    assert alice["full_sync_pending"] is False and alice["sync_configured"] is False
    assert profiles["bob"]["open"] is False and profiles["bob"]["collection_exists"] is False
    r = client.get("/v1/admin/profiles", headers=auth(alice_admin_token))
    assert [p["profile"] for p in r.json()["profiles"]] == ["alice"]


def test_admin_metrics_and_audit(
    client: TestClient, admin_token: str, alice_admin_token: str, seed_notes: Seeder
) -> None:
    seed_notes(2)
    r = client.get("/v1/admin/metrics", headers=auth(admin_token))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert 'ankido_http_requests_total{route="/v1/p/{profile}/notes",status="200"} 1' in r.text
    assert "ankido_http_request_seconds_bucket" in r.text

    r = client.get("/v1/admin/audit?limit=1", headers=auth(admin_token))
    entry = r.json()["entries"][0]
    assert entry["action"] == "notes" and entry["count"] == 2 and entry["outcome"] == "ok"
    assert entry["profile"] == "alice" and entry["token_id"]
    assert set(entry) >= {"id", "ts", "token_id", "profile", "action", "count", "outcome", "detail"}
    # profile-bound admin only sees its own profile
    r = client.get("/v1/admin/audit", headers=auth(alice_admin_token))
    assert {e["profile"] for e in r.json()["entries"]} == {"alice"}
    assert_error(
        client.get("/v1/admin/audit?limit=0", headers=auth(admin_token)), 422, "validation_error"
    )


def test_backup_endpoint(
    client: TestClient, admin_token: str, alice_admin_token: str, store: Store
) -> None:
    r = client.post("/v1/p/alice/backup", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    from pathlib import Path

    path = Path(r.json()["path"])
    assert path.is_file() and path.parent.name == "backups" and "-manual" in path.name
    assert store.audit_tail(1, profile="alice")[0]["action"] == "backup"
    assert client.post("/v1/p/alice/backup", headers=auth(alice_admin_token)).status_code == 200
    assert_error(client.post("/v1/p/bob/backup", headers=auth(alice_admin_token)), 403, "forbidden")


# ---- middleware ----------------------------------------------------------------------------


def test_payload_too_large_is_413(client: TestClient, alice_token: str) -> None:
    big = b'{"deck":"D","model":"Basic","notes":[],"tags":["' + b"x" * 70_000 + b'"]}'
    r = client.post(
        "/v1/p/alice/notes",
        content=big,
        headers={**auth(alice_token), "Content-Type": "application/json"},
    )
    assert_error(r, 413, "payload_too_large")


def test_chunked_post_without_content_length_is_411(client: TestClient, alice_token: str) -> None:
    r = client.post(
        "/v1/p/alice/notes",
        content=iter([b"{}"]),
        headers={**auth(alice_token), "Content-Type": "application/json"},
    )
    assert_error(r, 411, "length_required")


def test_unexpected_exception_in_op_is_500_with_error_shape(
    client: TestClient, alice_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(session: Session) -> list[dict[str, Any]]:
        raise RuntimeError("secret details")

    monkeypatch.setattr(ops, "get_decks", boom)
    r = client.get("/v1/p/alice/decks", headers=auth(alice_token))
    err = assert_error(r, 500, "internal_error")
    assert err["retryable"] is False
    assert "secret details" not in err["message"]


def test_rate_limit_cors_and_trusted_proxy(
    make_config: ConfigMaker, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = make_config(
        server={
            "rate_limits": {"read": {"per_minute": 60, "burst": 2}},
            "cors_origins": ["https://reader.example"],
            "trusted_proxies": ["testclient"],
        }
    )
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"read"}))
    with caplog.at_level(logging.INFO, logger="ankido.http"), TestClient(create_app(cfg)) as c:
        assert c.get("/v1/p/alice/decks", headers=auth(raw)).status_code == 200
        proxied = c.get(
            "/v1/p/alice/decks",
            headers={**auth(raw), "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )
        assert proxied.status_code == 200
        r = c.get("/v1/p/alice/decks", headers=auth(raw))
        err = assert_error(r, 429, "rate_limited")
        assert err["retryable"] is True and err["details"]["retry_after_seconds"] > 0
        assert int(r.headers["retry-after"]) >= 1
        pre = c.options(
            "/v1/p/alice/decks",
            headers={
                "Origin": "https://reader.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == "https://reader.example"
    clients = [
        getattr(rec, "extra_fields", {}).get("client")
        for rec in caplog.records
        if rec.name == "ankido.http" and rec.getMessage() == "request"
    ]
    assert "203.0.113.9" in clients and "testclient" in clients


def test_trusted_proxy_cidr_matching(
    make_config: ConfigMaker, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = make_config(server={"trusted_proxies": ["10.0.0.0/8", "not-a-network", "1.2.3.4"]})
    fwd = {"CF-Connecting-IP": "198.51.100.7"}
    with caplog.at_level(logging.INFO, logger="ankido.http"):
        # one app per client: a lifespan (and its worker threads) can only be started once
        with TestClient(create_app(cfg), client=("10.1.2.3", 5000)) as trusted:
            assert trusted.get("/healthz", headers=fwd).status_code == 200
        with TestClient(create_app(cfg), client=("192.0.2.9", 5000)) as untrusted:
            assert untrusted.get("/healthz", headers=fwd).status_code == 200
    clients = [
        getattr(rec, "extra_fields", {}).get("client")
        for rec in caplog.records
        if rec.name == "ankido.http" and rec.getMessage() == "request"
    ]
    assert clients == ["198.51.100.7", "192.0.2.9"]


def test_models_lists_fields(client: TestClient, alice_read_token: str) -> None:
    r = client.get("/v1/p/alice/models", headers=auth(alice_read_token))
    assert r.status_code == 200, r.text
    models = {m["name"]: m for m in r.json()["models"]}
    assert models["Basic"]["fields"] == ["Front", "Back"]
    assert models["Basic"]["cloze"] is False
    assert models["Cloze"]["cloze"] is True
    assert "Card 1" in models["Basic"]["templates"]
    assert (
        client.get(
            "/v1/p/alice/models",
            headers={**auth(alice_read_token), "If-None-Match": r.headers["etag"]},
        ).status_code
        == 304
    )


def test_search_notes(client: TestClient, alice_token: str, seed_notes: Seeder) -> None:
    seed_notes(3, deck="Lang::Dutch", prefix="huis")
    client.post(
        "/v1/p/alice/notes",
        json={
            "deck": "Other",
            "model": "Basic",
            "notes": [{"fields": {"Front": "<b>kat</b>", "Back": "cat"}, "tags": ["pets"]}],
        },
        headers=auth(alice_token),
    )
    r = client.get("/v1/p/alice/notes", params={"query": "tag:pets"}, headers=auth(alice_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["next_offset"] is None
    note = body["notes"][0]
    assert note["fields"] == {"Front": "kat", "Back": "cat"}
    assert note["decks"] == ["Other"] and note["tags"] == ["pets"] and note["model"] == "Basic"

    html = client.get(
        "/v1/p/alice/notes",
        params={"query": "tag:pets", "render": "html"},
        headers=auth(alice_token),
    ).json()
    assert html["notes"][0]["fields"]["Front"] == "<b>kat</b>"

    page = client.get(
        "/v1/p/alice/notes",
        params={"query": '"deck:Lang::Dutch"', "limit": 2},
        headers=auth(alice_token),
    ).json()
    assert page["total"] == 3 and len(page["notes"]) == 2 and page["next_offset"] == 2
    ids = [n["note_id"] for n in page["notes"]]
    assert ids == sorted(ids, reverse=True)
    rest = client.get(
        "/v1/p/alice/notes",
        params={"query": '"deck:Lang::Dutch"', "limit": 2, "offset": 2},
        headers=auth(alice_token),
    ).json()
    assert len(rest["notes"]) == 1 and rest["next_offset"] is None

    assert_error(
        client.get("/v1/p/alice/notes", params={"query": "("}, headers=auth(alice_token)),
        400,
        "invalid_search",
    )
    assert_error(
        client.get("/v1/p/alice/notes", headers=auth(alice_token)), 422, "validation_error"
    )


def test_search_notes_needs_read(client: TestClient, store: Store) -> None:
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"add"}))
    r = client.get("/v1/p/alice/notes", params={"query": "deck:*"}, headers=auth(raw))
    assert_error(r, 403, "forbidden")
