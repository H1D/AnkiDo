# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end against a real AnkiWeb account.

Runs only when ``ANKIDO_E2E_ENV`` points at an env-file with ``ANKIWEB_USERNAME`` /
``ANKIWEB_PASSWORD`` for a *throwaway* account. The flow is the one the spec asks for:
add → sync → appears in queue → grade → sync → the grade is visible after a fresh full download
into a second, empty profile.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ankido.api.app import create_app
from ankido.config import load_config
from ankido.store import Store

E2E_ENV = os.environ.get("ANKIDO_E2E_ENV")
pytestmark = pytest.mark.skipif(not E2E_ENV, reason="ANKIDO_E2E_ENV not set")

DECK = "Ankido::Test"


@pytest.fixture(scope="module")
def e2e(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    assert E2E_ENV
    root = tmp_path_factory.mktemp("e2e")
    data = root / "data"
    cfg = {
        "data_dir": str(data),
        "server": {
            "idle_close_seconds": 0,
            "rate_limits": {"sync": {"per_minute": 60, "burst": 30}},
        },
        "profiles": {
            "main": {
                "collection": str(data / "main" / "collection.anki2"),
                "credentials": E2E_ENV,
                "autosync": "off",
            },
            "verify": {
                "collection": str(data / "verify" / "collection.anki2"),
                "credentials": E2E_ENV,
                "autosync": "off",
            },
        },
    }
    cfg_path = root / "ankido.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    config = load_config(cfg_path)
    store = Store(config.state_db_path)
    admin, _ = store.create_token(profile=None, scopes=frozenset({"admin"}), name="e2e")
    store.close()
    app = create_app(config)
    with TestClient(app) as client:
        yield {"client": client, "h": {"Authorization": f"Bearer {admin}"}, "root": root}


def _post(e2e: dict[str, Any], path: str, body: dict[str, Any] | None = None) -> Any:
    r = e2e["client"].post(path, headers=e2e["h"], json=body)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def _get(e2e: dict[str, Any], path: str, **params: Any) -> Any:
    r = e2e["client"].get(path, headers=e2e["h"], params=params)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def _shim(
    e2e: dict[str, Any], profile: str, action: str, params: dict[str, Any] | None = None
) -> Any:
    r = e2e["client"].post(
        f"/api/{profile}",
        headers=e2e["h"],
        json={"action": action, "version": 6, "params": params or {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None, body
    return body["result"]


def test_add_sync_review_sync_verify(e2e: dict[str, Any]) -> None:
    run = uuid.uuid4().hex[:8]
    tag = f"ankido-e2e-{run}"

    # 1. first sync of a fresh profile. Three legitimate outcomes: nothing to do, a bootstrap
    #    download (remote has cards), or - for an account that has no cards yet - AnkiWeb demands a
    #    full upload, which Ankido refuses implicitly; the admin force_full call is the way through.
    r = e2e["client"].post("/v1/p/main/sync", headers=e2e["h"])
    if r.status_code == 409:
        err = r.json()["error"]
        assert err["code"] == "sync_required_full", err
        assert err["details"]["required"] == "full_upload", err
        forced = _post(e2e, "/v1/p/main/sync", {"force_full": "upload", "confirm": "main"})
        assert forced["outcome"] == "uploaded", forced
        audit = _get(e2e, "/v1/admin/audit", limit=5)["entries"]
        assert any(a["action"] == "force_full_upload" and a["outcome"] == "uploaded" for a in audit)
    else:
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] in ("no_changes", "merged", "downloaded"), r.json()

    # 2. add two notes, one with audio
    wav = base64.b64encode(b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(64)).decode()
    added = _post(
        e2e,
        "/v1/p/main/notes",
        {
            "deck": DECK,
            "model": "Basic",
            "tags": [tag],
            "notes": [
                {
                    "client_id": f"{run}-1",
                    "fields": {"Front": f"e2e front {run}", "Back": "back one"},
                    "audio": [{"filename": f"e2e-{run}.wav", "data": wav, "fields": ["Front"]}],
                },
                {
                    "client_id": f"{run}-2",
                    "fields": {"Front": f"e2e second {run}", "Back": "back two"},
                },
            ],
        },
    )["results"]
    assert [r["status"] for r in added] == ["added", "added"], added
    assert added[0]["media"] == [f"e2e-{run}.wav"]
    card_id = added[0]["card_ids"][0]

    # idempotent re-delivery of the same note batch
    again = _post(
        e2e,
        "/v1/p/main/notes",
        {
            "deck": DECK,
            "model": "Basic",
            "notes": [
                {"client_id": f"{run}-1", "fields": {"Front": f"e2e front {run}", "Back": "x"}}
            ],
        },
    )["results"]
    assert again[0]["status"] == "added" and again[0].get("replayed") is True

    # 3. sync: incremental, never full
    synced = _post(e2e, "/v1/p/main/sync")
    assert synced["outcome"] == "merged", synced
    assert synced["local_replaced"] is False
    assert synced["media"] == "synced"

    # 4. the cards are in the queue, ready to draw
    queue = _get(e2e, "/v1/p/main/queue", decks=DECK, limit=50)
    ids = {c["card_id"] for c in queue["cards"]}
    assert card_id in ids
    card = next(c for c in queue["cards"] if c["card_id"] == card_id)
    assert card["q"] == f"e2e front {run}"
    assert card["media"] == [f"e2e-{run}.wav"]
    assert len(card["next"]) == 4

    # 5. grade it with an offline-style timestamp (one hour ago), then replay the same batch
    answered_at = int(time.time()) - 3600
    body = {
        "reviews": [
            {
                "client_id": f"{run}-r1",
                "card_id": card_id,
                "ease": 3,
                "answered_at": answered_at,
                "time_ms": 4200,
            }
        ]
    }
    first_res = _post(e2e, "/v1/p/main/reviews", body)["results"][0]
    assert first_res["status"] == "applied", first_res
    replay = _post(e2e, "/v1/p/main/reviews", body)["results"][0]
    assert replay["status"] == "duplicate"
    assert replay["interval_days"] == first_res["interval_days"]

    # 6. sync the grade up
    synced = _post(e2e, "/v1/p/main/sync")
    assert synced["outcome"] == "merged", synced

    # 7. a brand-new profile bootstraps by full download and sees the review
    verify = _post(e2e, "/v1/p/verify/sync")
    assert verify["outcome"] == "downloaded", verify
    assert verify["local_replaced"] is True
    info = _shim(e2e, "verify", "cardsInfo", {"cards": [card_id]})[0]
    assert info["cardId"] == card_id
    assert info["reps"] == 1
    stats = _get(e2e, "/v1/p/verify/stats", days=3)
    assert sum(stats["reviewed_by_day"].values()) >= 1
    media_dir = Path(e2e["root"]) / "data" / "verify" / "collection.media"
    assert (media_dir / f"e2e-{run}.wav").is_file()

    # 8. cleanup: remove this run's notes and push that up, so the account stays small
    nids = _shim(e2e, "main", "findNotes", {"query": f"tag:{tag}"})
    assert len(nids) == 2
    _shim(e2e, "main", "deleteNotes", {"notes": nids})
    synced = _post(e2e, "/v1/p/main/sync")
    assert synced["outcome"] == "merged"


def test_full_sync_is_never_implicit(e2e: dict[str, Any]) -> None:
    """Force-full needs admin + confirm; without confirm it is refused before touching AnkiWeb."""
    r = e2e["client"].post("/v1/p/main/sync", headers=e2e["h"], json={"force_full": "upload"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "confirmation_required"
