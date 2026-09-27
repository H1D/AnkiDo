# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP at /mcp/p/{profile}: the SDK client against the real app, both protocol eras."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, TextContent

from ankido.api.app import create_app
from ankido.collection.session import Session, SyncOutcome, SyncResult
from ankido.store import Store
from conftest import ConfigMaker, auth

pytestmark = pytest.mark.anyio

# "legacy" = initialize handshake (2025-11-25 and older), "2026-07-28" = stateless, no handshake.
MODES = ["legacy", "2026-07-28"]
ORIGIN = "https://anki.example.com"
ALL_TOOLS = {
    "list_decks",
    "list_note_types",
    "search_notes",
    "get_stats",
    "get_queue",
    "sync_status",
    "add_notes",
    "submit_reviews",
    "sync",
}
READ_TOOLS = {
    "list_decks",
    "list_note_types",
    "search_notes",
    "get_stats",
    "get_queue",
    "sync_status",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mcp_app(make_config: ConfigMaker) -> FastAPI:
    return create_app(make_config(server={"public_url": ORIGIN}))


@asynccontextmanager
async def running(app: FastAPI) -> AsyncGenerator[None]:
    # httpx2's ASGI transport does not drive the lifespan; enter it by hand.
    async with app.router.lifespan_context(app):
        yield


@asynccontextmanager
async def mcp_client(
    app: FastAPI, token: str, mode: str = "2026-07-28", profile: str = "alice"
) -> AsyncGenerator[Client]:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {token}"},
    )
    url = f"{ORIGIN}/mcp/p/{profile}"
    async with http, Client(streamable_http_client(url, http_client=http), mode=mode) as c:
        yield c


def data(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result
    assert result.structured_content is not None
    return result.structured_content


def error(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error, result
    block = result.content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)["error"]


def tokens(store: Store, **scopes: str) -> dict[str, str]:
    return {
        name: store.create_token(profile="alice", scopes=frozenset(s.split(",")))[0]
        for name, s in scopes.items()
    }


# ---- listing and scopes -----------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_tool_list_follows_token_scopes(mcp_app: FastAPI, store: Store, mode: str) -> None:
    t = tokens(store, ro="read", full="read,add,review,sync", adm="admin", rev="read,review")
    async with running(mcp_app):
        async with mcp_client(mcp_app, t["ro"], mode) as c:
            assert {x.name for x in (await c.list_tools()).tools} == READ_TOOLS
        async with mcp_client(mcp_app, t["rev"], mode) as c:
            assert {x.name for x in (await c.list_tools()).tools} == READ_TOOLS | {"submit_reviews"}
        async with mcp_client(mcp_app, t["full"], mode) as c:
            listed = {x.name: x for x in (await c.list_tools()).tools}
            assert set(listed) == ALL_TOOLS
            ann = listed["search_notes"].annotations
            assert ann is not None and ann.read_only_hint is True
            ann = listed["add_notes"].annotations
            assert ann is not None and ann.read_only_hint is False
            assert ann.destructive_hint is False
        async with mcp_client(mcp_app, t["adm"], mode) as c:
            assert {x.name for x in (await c.list_tools()).tools} == ALL_TOOLS


@pytest.mark.parametrize("mode", MODES)
async def test_calls_are_checked_even_if_unlisted(
    mcp_app: FastAPI, store: Store, mode: str
) -> None:
    t = tokens(store, ro="read")
    async with running(mcp_app), mcp_client(mcp_app, t["ro"], mode) as c:
        err = error(
            await c.call_tool(
                "add_notes", {"deck": "X", "notes": [{"fields": {"Front": "a", "Back": "b"}}]}
            )
        )
        assert err["code"] == "forbidden"
        assert err["details"] == {"required_scope": "add", "profile": "alice"}


# ---- the tools ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_add_search_queue_review(mcp_app: FastAPI, store: Store, mode: str) -> None:
    t = tokens(store, full="read,add,review")
    async with running(mcp_app), mcp_client(mcp_app, t["full"], mode) as c:
        types = data(await c.call_tool("list_note_types", {}))["note_types"]
        assert {"name": "Basic", "fields": ["Front", "Back"]}.items() <= next(
            m for m in types if m["name"] == "Basic"
        ).items()

        added = data(
            await c.call_tool(
                "add_notes",
                {
                    "deck": "Dutch::Common",
                    "tags": ["src:chat"],
                    "notes": [
                        {"client_id": "n1", "fields": {"Front": "het huis", "Back": "house"}},
                        {"client_id": "n2", "fields": {"Front": "de kat", "Back": "cat"}},
                    ],
                },
            )
        )["results"]
        assert [r["status"] for r in added] == ["added", "added"]
        again = data(
            await c.call_tool(
                "add_notes",
                {"deck": "Dutch::Common", "notes": [{"fields": {"Front": "het huis"}}]},
            )
        )["results"]
        assert again[0]["status"] == "skipped_duplicate"

        found = data(await c.call_tool("search_notes", {"query": "tag:src:chat", "limit": 1}))
        assert found["total"] == 2 and found["next_offset"] == 1
        assert found["notes"][0]["fields"]["Front"] == "de kat"

        decks = data(await c.call_tool("list_decks", {}))["decks"]
        assert "Dutch::Common" in {d["name"] for d in decks}

        queue = data(await c.call_tool("get_queue", {"decks": ["Dutch"], "limit": 5}))
        assert queue["counts"]["new"] == 2
        card = queue["cards"][0]
        assert {"card_id", "q", "a", "next"} <= set(card)
        assert "<" not in card["q"]  # plain text

        review = {"card_id": card["card_id"], "ease": 3, "client_id": "r1"}
        first = data(await c.call_tool("submit_reviews", {"reviews": [review]}))["results"]
        assert first[0]["status"] == "applied"
        dup = data(await c.call_tool("submit_reviews", {"reviews": [review]}))["results"]
        assert dup[0]["status"] == "duplicate"

        stats = data(await c.call_tool("get_stats", {"days": 7}))
        assert stats["reviewed_today"] == 1 and "day_rollover_hour" in stats
        assert "last_sync_at" in data(await c.call_tool("sync_status", {}))

    rows = store.audit_tail(10, profile="alice")
    assert {(r["action"], r["detail"]) for r in rows} >= {
        ("notes", "via=mcp"),
        ("reviews", "via=mcp"),
    }


async def test_media_by_data_but_never_by_path(mcp_app: FastAPI, store: Store) -> None:
    t = tokens(store, full="read,add")
    audio = base64.b64encode(b"ID3fake").decode()
    async with running(mcp_app), mcp_client(mcp_app, t["full"]) as c:
        ok = data(
            await c.call_tool(
                "add_notes",
                {
                    "deck": "D",
                    "notes": [
                        {
                            "fields": {"Front": "huis", "Back": "house"},
                            "audio": [{"filename": "huis.mp3", "data": audio}],
                        }
                    ],
                },
            )
        )["results"][0]
        assert ok["status"] == "added" and ok["media"] == ["huis.mp3"]

        bad = await c.call_tool(
            "add_notes",
            {
                "deck": "D",
                "notes": [{"fields": {"Front": "x"}, "audio": [{"path": "/etc/passwd"}]}],
            },
        )
        assert bad.is_error
        block = bad.content[0]
        assert isinstance(block, TextContent) and "path" in block.text


async def test_errors_are_api_shaped(mcp_app: FastAPI, store: Store) -> None:
    t = tokens(store, full="read,add")
    async with running(mcp_app), mcp_client(mcp_app, t["full"]) as c:
        err = error(await c.call_tool("search_notes", {"query": "("}))
        assert err["code"] == "invalid_search" and err["retryable"] is False
        err = error(await c.call_tool("get_queue", {"decks": ["Nope"]}))
        assert err["code"] == "deck_not_found"
        res = data(
            await c.call_tool(
                "add_notes",
                {"deck": "D", "model": "Basic", "notes": [{"fields": {"Nope": "x"}}]},
            )
        )["results"][0]
        assert res["status"] == "error" and res["error"]["code"] == "unknown_field"


async def test_sync_tool(mcp_app: FastAPI, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_sync(self: Session, *, force_full: str | None = None) -> SyncResult:
        assert force_full is None
        return SyncResult(
            outcome=SyncOutcome.NO_CHANGES,
            local_replaced=False,
            server_message="",
            media="skipped",
            duration_ms=1,
        )

    monkeypatch.setattr(Session, "sync", fake_sync)
    t = tokens(store, s="read,sync")
    async with running(mcp_app), mcp_client(mcp_app, t["s"]) as c:
        assert data(await c.call_tool("sync", {}))["outcome"] == "no_changes"


async def test_review_session_prompt(mcp_app: FastAPI, store: Store) -> None:
    t = tokens(store, ro="read")
    async with running(mcp_app), mcp_client(mcp_app, t["ro"]) as c:
        prompts = (await c.list_prompts()).prompts
        assert [p.name for p in prompts] == ["review_session"]
        got = await c.get_prompt("review_session", {"deck": "Dutch"})
        block = got.messages[0].content
        assert isinstance(block, TextContent)
        assert 'decks=["Dutch"]' in block.text and "submit_reviews" in block.text


async def test_trailing_slash_and_other_profile(mcp_app: FastAPI, store: Store) -> None:
    bob, _ = store.create_token(profile="bob", scopes=frozenset({"read"}))
    async with running(mcp_app), mcp_client(mcp_app, bob, profile="bob/") as c:
        assert {x.name for x in (await c.list_tools()).tools} == READ_TOOLS


# ---- HTTP-level auth errors --------------------------------------------------------------

INIT = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def test_missing_token_points_at_resource_metadata(mcp_app: FastAPI) -> None:
    with TestClient(mcp_app, base_url=ORIGIN) as client:
        r = client.post("/mcp/p/alice", json=INIT)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp/p/alice",'
        ' scope="read add review"'
    )


def test_invalid_token_says_invalid_token(mcp_app: FastAPI) -> None:
    with TestClient(mcp_app, base_url=ORIGIN) as client:
        r = client.post("/mcp/p/alice", json=INIT, headers=auth("akd_00000000_nope"))
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_without_public_url_the_401_explains_the_fix(make_config: ConfigMaker) -> None:
    with TestClient(create_app(make_config())) as client:
        r = client.post("/mcp/p/alice", json=INIT)
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "oauth_not_configured"
    assert any("server.public_url" in step for step in err["details"]["fix"])
    assert any("ankido token create" in step for step in err["details"]["fix"])
    assert "server.public_url" in r.headers["www-authenticate"]


def test_wrong_host_is_reported(mcp_app: FastAPI) -> None:
    with TestClient(mcp_app, base_url="http://192.168.1.5:8765") as client:
        r = client.post("/mcp/p/alice", json=INIT)
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "oauth_public_url_mismatch"
    assert err["details"]["expected"] == ORIGIN
    assert err["details"]["seen"] == "http://192.168.1.5:8765"


def test_static_token_works_without_public_url(make_config: ConfigMaker, store: Store) -> None:
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"read"}))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "curl", "version": "0"},
        },
    }
    headers = {**auth(raw), "Accept": "application/json, text/event-stream"}
    with TestClient(create_app(make_config()), base_url="http://127.0.0.1:8765") as client:
        r = client.post("/mcp/p/alice", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "ankido"


def test_profile_binding_and_unknown_profile(mcp_app: FastAPI, store: Store) -> None:
    alice, _ = store.create_token(profile="alice", scopes=frozenset({"read"}))
    root, _ = store.create_token(profile=None, scopes=frozenset({"admin"}))
    with TestClient(mcp_app, base_url=ORIGIN) as client:
        r = client.post("/mcp/p/bob", json=INIT, headers=auth(alice))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        r = client.post("/mcp/p/nobody", json=INIT, headers=auth(root))
        assert r.status_code == 404 and r.json()["error"]["code"] == "profile_not_found"
