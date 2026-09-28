# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the AnkiConnect v6 shim (``POST /api/{profile}``)."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ankido.api.shim import action_names
from ankido.store import Store
from conftest import auth

Caller = Callable[..., Any]

CARDS_INFO_KEYS = {
    "cardId", "fields", "fieldOrder", "question", "answer", "modelName", "ord", "deckName",
    "css", "factor", "interval", "note", "type", "queue", "due", "reps", "lapses", "left",
    "mod", "nextReviews", "flags",
}  # fmt: skip


@pytest.fixture
def call(client: TestClient, alice_token: str) -> Caller:
    def _call(
        action: str,
        params: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        profile: str = "alice",
        version: int = 6,
        key_in_body: bool = False,
    ) -> Any:
        tok = token or alice_token
        body: dict[str, Any] = {"action": action, "version": version, "params": params or {}}
        headers: dict[str, str] = {}
        if key_in_body:
            body["key"] = tok
        else:
            headers = auth(tok)
        r = client.post(f"/api/{profile}", json=body, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()

    return _call


def ok(resp: dict[str, Any]) -> Any:
    assert resp["error"] is None, resp
    return resp["result"]


def err(resp: dict[str, Any]) -> str:
    assert resp["result"] is None, resp
    assert isinstance(resp["error"], str)
    return resp["error"]


def note(front: str, back: str = "b", *, model: str = "Basic", **extra: Any) -> dict[str, Any]:
    return {
        "deckName": "Shim",
        "modelName": model,
        "fields": {"Front": front, "Back": back},
        "tags": ["shim"],
        **extra,
    }


def add(call: Caller, front: str) -> tuple[int, int]:
    """Add a Basic note; return (note id, card id)."""
    nid = ok(call("addNote", {"note": note(front)}))
    assert isinstance(nid, int)
    cids = ok(call("findCards", {"query": f"nid:{nid}"}))
    assert len(cids) == 1
    return nid, cids[0]


# ---- transport / auth -------------------------------------------------------------------


def test_version_and_request_permission(call: Caller) -> None:
    assert ok(call("version")) == 6
    assert ok(call("requestPermission")) == {
        "permission": "granted",
        "requireApiKey": True,
        "version": 6,
    }


def test_key_in_body_and_bearer_both_work(call: Caller) -> None:
    assert ok(call("version", key_in_body=True)) == 6
    assert ok(call("version")) == 6


def test_wrong_or_missing_key_is_string_error_with_http_200(client: TestClient) -> None:
    r = client.post(
        "/api/alice", json={"action": "version", "version": 6, "key": "akd_00000000_" + "x" * 43}
    )
    assert r.status_code == 200
    assert r.json() == {"result": None, "error": "invalid token"}
    r = client.post("/api/alice", json={"action": "version", "version": 6})
    assert r.status_code == 200
    assert r.json()["result"] is None and r.json()["error"].startswith("missing token")
    r = client.post("/api/alice", json={"action": "version", "version": 6, "key": ""})
    assert r.json()["error"].startswith("missing token")


def test_unknown_profile_and_unknown_action(call: Caller) -> None:
    assert err(call("version", profile="zed")) == "profile 'zed' not found"
    assert err(call("bogusAction")) == "unsupported action: bogusAction"
    assert err(call("")) == "unsupported action: "


def test_invalid_json_and_non_object_are_400(client: TestClient, alice_token: str) -> None:
    r = client.post(
        "/api/alice",
        content=b"{not json",
        headers={**auth(alice_token), "Content-Type": "application/json"},
    )
    assert r.status_code == 400 and r.json() == {"result": None, "error": "invalid JSON"}
    r = client.post("/api/alice", json=[1, 2], headers=auth(alice_token))
    assert r.status_code == 400 and r.json()["error"] == "request must be an object"


def test_version_below_6_returns_bare_result(call: Caller) -> None:
    assert call("version", version=5) == 6
    assert call("deckNames", version=4) == []


def test_scope_enforced_per_action(
    call: Caller, alice_read_token: str, bob_token: str, alice_token: str
) -> None:
    assert ok(call("version", token=alice_read_token)) == 6
    assert err(call("createDeck", {"deck": "X"}, token=alice_read_token)).startswith(
        "token lacks scope 'add'"
    )
    assert err(call("version", token=bob_token)) == "token lacks scope 'read' on profile 'alice'"
    assert err(call("deleteNotes", {"notes": []}, token=alice_token)).startswith(
        "token lacks scope 'delete'"
    )


# ---- decks / models ---------------------------------------------------------------------


def test_deck_actions(call: Caller, alice_admin_token: str) -> None:
    did = ok(call("createDeck", {"deck": "Shim::Sub"}))
    assert isinstance(did, int)
    assert {"Shim", "Shim::Sub"} <= set(ok(call("deckNames")))
    assert ok(call("deckNamesAndIds"))["Shim::Sub"] == did
    assert "name" in ok(call("getDeckConfig", {"deck": "Shim::Sub"}))
    assert err(call("getDeckConfig", {"deck": "Nope"})) == "deck was not found"
    tree = ok(call("deckDueTree"))
    assert [(d["name"], d["level"]) for d in tree] == [("Shim", 1), ("Shim::Sub", 2)]
    assert (
        ok(call("deleteDecks", {"decks": ["Shim::Sub", "Nope"]}, token=alice_admin_token)) is None
    )
    assert "Shim::Sub" not in ok(call("deckNames"))


def test_model_actions(call: Caller) -> None:
    assert "Basic" in ok(call("modelNames"))
    assert isinstance(ok(call("modelNamesAndIds"))["Basic"], int)
    assert ok(call("modelFieldNames", {"modelName": "Basic"})) == ["Front", "Back"]
    assert ".card" in ok(call("modelStyling", {"modelName": "Basic"}))["css"]
    templates = ok(call("modelTemplates", {"modelName": "Basic"}))
    assert set(templates) == {"Card 1"} and set(templates["Card 1"]) == {"Front", "Back"}
    for action in ("modelFieldNames", "modelStyling", "modelTemplates"):
        assert err(call(action, {"modelName": "Nope"})) == "model was not found"


# ---- notes --------------------------------------------------------------------------------


def test_add_note_duplicate_and_allow_duplicate(call: Caller) -> None:
    nid = ok(call("addNote", {"note": note("huis")}))
    assert isinstance(nid, int)
    assert (
        err(call("addNote", {"note": note("HUIS")}))
        == "cannot create note because it is a duplicate"
    )
    nid2 = ok(call("addNote", {"note": note("huis", options={"allowDuplicate": True})}))
    assert isinstance(nid2, int) and nid2 != nid
    assert err(call("addNote", {"note": note("x", model="Nope")})) == "model 'Nope' not found"
    info = ok(call("notesInfo", {"notes": [nid]}))[0]
    assert info["tags"] == ["shim"] and info["modelName"] == "Basic"


def test_add_notes_and_can_add_notes(call: Caller) -> None:
    res = ok(call("addNotes", {"notes": [note("een"), note("een"), note("twee", model="Nope")]}))
    assert isinstance(res[0], int) and res[1:] == [None, None]
    can = ok(
        call(
            "canAddNotes",
            {"notes": [note("drie"), note("EEN"), note("x", model="Nope"), note("")]},
        )
    )
    assert can == [True, False, False, False]


def test_find_cards_cards_info_and_related_lookups(call: Caller) -> None:
    nid, cid = add(call, "kaas")
    assert ok(call("findCards", {"query": "deck:Shim"})) == [cid]
    info = ok(call("cardsInfo", {"cards": [cid, 1]}))
    assert info[1] == {}
    assert set(info[0]) == CARDS_INFO_KEYS
    assert info[0]["fields"]["Front"] == {"value": "kaas", "order": 0}
    assert info[0]["deckName"] == "Shim" and info[0]["note"] == nid
    assert "kaas" in info[0]["question"] and "<hr id=answer>" in info[0]["answer"]
    assert ok(call("cardsModTime", {"cards": [cid]}))[0]["cardId"] == cid
    assert ok(call("cardsToNotes", {"cards": [cid, cid, 1]})) == [nid]
    assert ok(call("findNotes", {"query": "deck:Shim"})) == [nid]
    ninfo = ok(call("notesInfo", {"notes": [nid, 1]}))
    assert ninfo[0]["noteId"] == nid and ninfo[0]["cards"] == [cid] and ninfo[1] == {}
    assert err(call("cardsInfo", {"cards": "notalist"})) == "cards must be a list"
    assert err(call("findCards", {"query": '"bad'})).startswith("invalid search")
    assert ok(call("getDecks", {"cards": [cid, 1]})) == {"Shim": [cid]}
    assert ok(call("changeDeck", {"cards": [cid], "deck": "Moved"})) is None
    assert ok(call("getDecks", {"cards": [cid]})) == {"Moved": [cid]}


def test_answer_cards_redelivery_grades_twice(call: Caller, store: Store) -> None:
    """Documented shim limitation: answerCards has no idempotency key."""
    _, cid = add(call, "twice")
    assert ok(call("answerCards", {"answers": [{"cardId": cid, "ease": 3, "timeMs": 1000}]})) == [
        True
    ]
    assert ok(call("answerCards", {"answers": [{"cardId": cid, "ease": 3}]})) == [True]
    history = ok(call("getIntervals", {"cards": [cid], "complete": True}))
    assert len(history[0]) == 2  # graded twice
    assert ok(call("getNumCardsReviewedToday")) == 2
    by_day = ok(call("getNumCardsReviewedByDay"))
    assert len(by_day) == 1 and by_day[0][1] == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", by_day[0][0])
    assert ok(call("answerCards", {"answers": [{"cardId": 1, "ease": 3}]})) == [False]
    assert ok(call("answerCards", {})) == []
    ivls = ok(call("getIntervals", {"cards": [cid, 1]}))
    assert ivls[0] >= 1 and ivls[1] == 0  # two Goods graduate a new card to a 1-day interval
    tail = store.audit_tail(10, profile="alice")
    assert [e["count"] for e in tail if e["action"] == "answerCards"] == [0, 0, 1, 1]


def test_tags_update_and_delete(call: Caller, alice_admin_token: str) -> None:
    nid, _ = add(call, "tagged")
    assert ok(call("addTags", {"notes": [nid], "tags": "extra another"})) is None
    assert {"shim", "extra", "another"} <= set(ok(call("getTags")))
    assert set(ok(call("notesInfo", {"notes": [nid]}))[0]["tags"]) == {"shim", "extra", "another"}
    assert ok(call("removeTags", {"notes": [nid], "tags": "extra"})) is None
    assert "extra" not in ok(call("notesInfo", {"notes": [nid]}))[0]["tags"]
    assert (
        ok(call("updateNoteFields", {"note": {"id": nid, "fields": {"Back": "changed"}}})) is None
    )
    assert ok(call("notesInfo", {"notes": [nid]}))[0]["fields"]["Back"]["value"] == "changed"
    assert err(call("updateNoteFields", {"note": {"id": nid, "fields": {"Nope": "x"}}})).startswith(
        "unknown field(s)"
    )
    assert err(call("updateNoteFields", {"note": {"id": 1, "fields": {"Back": "x"}}})) == (
        "note 1 not found"
    )
    assert ok(call("deleteNotes", {"notes": [nid]}, token=alice_admin_token)) is None
    assert ok(call("notesInfo", {"notes": [nid]})) == [{}]


def test_delete_notes_with_delete_scope(call: Caller, store: Store) -> None:
    nid, _ = add(call, "doomed")
    deleter, _ = store.create_token(profile="alice", scopes=frozenset({"delete"}))
    assert ok(call("deleteNotes", {"notes": [nid, 1]}, token=deleter)) is None
    assert ok(call("notesInfo", {"notes": [nid]})) == [{}]
    [row] = [e for e in store.audit_tail(10, profile="alice") if e["action"] == "delete_notes"]
    assert row["detail"].startswith(f"via=ankiconnect note_ids={nid} backup=collection-")


def test_suspend_due_set_due_date_and_forget(call: Caller) -> None:
    _, cid = add(call, "sched")
    assert ok(call("areDue", {"cards": [cid]})) == [False]  # new cards are not "due"
    assert ok(call("suspend", {"cards": [cid]})) is True
    assert ok(call("areSuspended", {"cards": [cid, 1]})) == [True, None]
    assert ok(call("unsuspend", {"cards": [cid]})) is True
    assert ok(call("areSuspended", {"cards": [cid]})) == [False]
    assert ok(call("answerCards", {"answers": [{"cardId": cid, "ease": 4}]})) == [True]
    assert ok(call("cardsInfo", {"cards": [cid]}))[0]["queue"] == 2
    assert ok(call("areDue", {"cards": [cid]})) == [False]
    assert ok(call("setDueDate", {"cards": [cid], "days": "0"})) is True
    assert ok(call("areDue", {"cards": [cid]})) == [True]
    assert ok(call("forgetCards", {"cards": [cid]})) is None
    assert ok(call("cardsInfo", {"cards": [cid]}))[0]["type"] == 0


def test_media_actions(call: Caller, alice_admin_token: str) -> None:
    data = base64.b64encode(b"ID3synthetic").decode()
    assert ok(call("storeMediaFile", {"filename": "tone.mp3", "data": data})) == "tone.mp3"
    assert ok(call("retrieveMediaFile", {"filename": "tone.mp3"})) == data
    assert ok(call("retrieveMediaFile", {"filename": "../nope.mp3"})) is False
    assert ok(call("getMediaFilesNames", {"pattern": "*.mp3"})) == ["tone.mp3"]
    assert ok(call("getMediaFilesNames")) == ["tone.mp3"]
    assert ok(call("getMediaDirPath")).endswith("collection.media")
    assert err(
        call("storeMediaFile", {"filename": "x.mp3", "url": "https://evil.org/x"})
    ).startswith("url host 'evil.org' is not in media.url_allowlist")
    assert err(call("deleteMediaFile", {"filename": "tone.mp3"})).startswith("token lacks scope")
    assert ok(call("deleteMediaFile", {"filename": "tone.mp3"}, token=alice_admin_token)) is None
    assert ok(call("getMediaFilesNames", {"pattern": "*.mp3"})) == []


def test_sync_and_profile_actions(call: Caller) -> None:
    assert ok(call("syncStatus"))["configured"] is False
    assert err(call("sync")) == "profile has no AnkiWeb credentials configured"
    assert ok(call("getProfiles")) == ["alice"]
    assert ok(call("getActiveProfile")) == "alice"
    assert ok(call("loadProfile", {"name": "alice"})) is True
    assert ok(call("loadProfile", {"name": "bob"})) is False


def test_multi(call: Caller) -> None:
    res = ok(
        call(
            "multi",
            {
                "actions": [
                    {"action": "version"},
                    {"action": "createDeck", "params": {"deck": "Multi"}},
                    {"action": "deckNames"},
                    {"action": "bogus"},
                ]
            },
        )
    )
    assert res[0] == {"result": 6, "error": None}
    assert isinstance(res[1]["result"], int)
    assert "Multi" in res[2]["result"]
    assert res[3] == {"result": None, "error": "unsupported action: bogus"}


def test_api_reflect_and_action_names(call: Caller) -> None:
    everything = ok(call("apiReflect"))
    assert everything["scopes"] == ["actions"]
    names = set(everything["actions"])
    assert {"version", "answerCards", "addNote", "addNotes", "cardsInfo", "sync"} <= names
    assert "multi" not in names
    assert ok(call("apiReflect", {"actions": ["version", "nope"]}))["actions"] == ["version"]
    assert {"multi", "answerCards", "version"} <= set(action_names())
    assert action_names() == sorted(action_names())
