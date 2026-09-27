# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth for MCP: metadata, DCR and metadata-document clients, consent, tokens, revocation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankido import oauth
from ankido.api.app import create_app
from ankido.oauth import ClientRegistry, OAuthError
from ankido.store import Store
from conftest import ConfigMaker, auth

ORIGIN = "https://anki.example.com"
RESOURCE = f"{ORIGIN}/mcp/p/alice"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CIMD_URL = "https://client.example.org/oauth/client.json"
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture
def app(make_config: ConfigMaker) -> FastAPI:
    # Several full sign-ins per test; the default oauth bucket (burst 10) is sized for one.
    limits = {"oauth": {"per_minute": 600, "burst": 100}}
    return create_app(make_config(server={"public_url": ORIGIN, "rate_limits": limits}))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as c:
        yield c


@pytest.fixture
def parent(store: Store) -> str:
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"read", "add", "review"}))
    return raw


def register(client: TestClient, **extra: Any) -> dict[str, Any]:
    body = {"redirect_uris": [CALLBACK], "client_name": "Claude", **extra}
    body.setdefault("token_endpoint_auth_method", "none")
    r = client.post("/oauth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def open_consent(
    client: TestClient,
    client_id: str,
    challenge: str,
    *,
    scope: str = "read add review",
    redirect_uri: str = CALLBACK,
    resource: str = RESOURCE,
) -> dict[str, str]:
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st4te",
            "scope": scope,
            "resource": resource,
        },
    )
    assert r.status_code == 200, r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    fields = dict(re.findall(r'name=(request_id|csrf) value="([^"]+)"', r.text))
    assert set(fields) == {"request_id", "csrf"}
    return fields


def approve(
    client: TestClient,
    form: dict[str, str],
    token: str,
    scopes: list[str],
    action: str = "approve",
) -> httpx2.Response:
    body = [*form.items(), ("token", token), ("action", action)]
    body += [("scope", s) for s in scopes]
    return client.post(
        "/oauth/authorize",
        content="&".join(f"{k}={v}" for k, v in body),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def query(r: httpx2.Response) -> dict[str, str]:
    # 303 from the consent POST (never re-post the pasted token), 302 from GET errors.
    assert r.status_code in (302, 303), r.text
    loc = r.headers["location"]
    return {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


def authorize(
    client: TestClient, parent: str, *, scopes: list[str] | None = None
) -> tuple[dict[str, Any], str, str]:
    reg = register(client)
    verifier, challenge = pkce()
    form = open_consent(client, reg["client_id"], challenge)
    q = query(approve(client, form, parent, scopes or ["read", "add"]))
    assert q["state"] == "st4te" and q["iss"] == ORIGIN
    return reg, q["code"], verifier


def exchange(client: TestClient, reg: dict[str, Any], code: str, verifier: str) -> httpx2.Response:
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )


def mcp_status(client: TestClient, token: str, profile: str = "alice") -> int:
    r = client.post(f"/mcp/p/{profile}", json=TOOLS_LIST, headers={**MCP_HEADERS, **auth(token)})
    return r.status_code


# ---- metadata ----------------------------------------------------------------------------


def test_metadata(client: TestClient) -> None:
    prm = client.get("/.well-known/oauth-protected-resource/mcp/p/alice").json()
    assert prm["resource"] == RESOURCE
    assert prm["authorization_servers"] == [ORIGIN]
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == ORIGIN
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["client_id_metadata_document_supported"] is True
    assert "none" in asm["token_endpoint_auth_methods_supported"]
    assert asm["registration_endpoint"] == f"{ORIGIN}/oauth/register"
    r = client.get("/.well-known/oauth-protected-resource/mcp/p/nobody")
    assert r.status_code == 404


def test_not_configured_is_explained(make_config: ConfigMaker) -> None:
    with TestClient(create_app(make_config()), base_url=ORIGIN) as c:
        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/mcp/p/alice",
        ):
            r = c.get(path)
            assert r.status_code == 404
            err = r.json()["error"]
            assert err["code"] == "oauth_not_configured"
            assert err["details"]["docs"].endswith("docs/mcp.md#oauth")
        r = c.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
        assert r.json()["error"]["code"] == "oauth_not_configured"


def test_plain_http_public_url_is_refused(make_config: ConfigMaker) -> None:
    app = create_app(make_config(server={"public_url": "http://anki.example.com"}))
    with TestClient(app, base_url="http://anki.example.com") as c:
        r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "oauth_public_url_mismatch"
    assert err["details"]["configured"] == "http://anki.example.com"


def test_host_mismatch_and_trusted_forwarded_host(make_config: ConfigMaker) -> None:
    app = create_app(make_config(server={"public_url": ORIGIN, "trusted_proxies": ["testclient"]}))
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 400
        assert r.json()["error"]["details"]["seen"] == "http://127.0.0.1:8765"
        r = c.get(
            "/.well-known/oauth-authorization-server",
            headers={"X-Forwarded-Host": "anki.example.com"},
        )
        assert r.status_code == 200


def test_public_url_must_be_an_origin(make_config: ConfigMaker) -> None:
    with pytest.raises(ValueError, match="without a path"):
        make_config(server={"public_url": "https://example.com/anki"})
    cfg = make_config(server={"public_url": "https://Example.com/"})
    assert cfg.server.public_url == "https://Example.com"


# ---- the full flow -----------------------------------------------------------------------


def test_dcr_consent_token_refresh_revoke(client: TestClient, store: Store, parent: str) -> None:
    reg, code, verifier = authorize(client, parent)
    r = exchange(client, reg, code, verifier)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == oauth.ACCESS_TTL
    assert tok["scope"] == "read add"
    access, refresh = tok["access_token"], tok["refresh_token"]

    assert mcp_status(client, access) == 200
    # Bound to one resource: not /v1, not another profile.
    assert client.get("/v1/p/alice/decks", headers=auth(access)).status_code == 401
    assert mcp_status(client, access, "bob") == 401

    # The code is single use.
    assert exchange(client, reg, code, verifier).json()["error"] == "invalid_grant"

    grant = next(t for t in store.list_tokens("alice") if t.kind == "oauth")
    assert grant.name == "oauth:Claude" and grant.scopes == {"read", "add"}

    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": reg["client_id"],
        },
    )
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != refresh
    # The old refresh token is dead; the old access token lives until it expires.
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": reg["client_id"],
        },
    )
    assert r.json()["error"] == "invalid_grant"
    assert mcp_status(client, access) == 200
    assert mcp_status(client, new["access_token"]) == 200

    # Revoking the pasted token disconnects the client.
    parent_id = parent.split("_")[1]
    assert store.revoke_token(parent_id)
    assert mcp_status(client, new["access_token"]) == 401
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": new["refresh_token"],
            "client_id": reg["client_id"],
        },
    )
    assert r.json()["error"] == "invalid_grant"

    actions = {(a["action"], a["outcome"]) for a in store.audit_tail(20)}
    assert {("oauth_consent", "granted"), ("oauth_token", "issued")} <= actions


def test_revoke_endpoint(client: TestClient, parent: str) -> None:
    reg, code, verifier = authorize(client, parent)
    tok = exchange(client, reg, code, verifier).json()
    r = client.post(
        "/oauth/revoke", data={"token": tok["refresh_token"], "client_id": reg["client_id"]}
    )
    assert r.status_code == 200
    assert mcp_status(client, tok["access_token"]) == 401


def test_pkce_redirect_and_resource_are_checked(client: TestClient, parent: str) -> None:
    reg, code, _ = authorize(client, parent)
    assert exchange(client, reg, code, "x" * 50).json()["error"] == "invalid_grant"

    reg, code, verifier = authorize(client, parent)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://evil.example/cb",
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r.json()["error"] == "invalid_grant"

    reg, code, verifier = authorize(client, parent)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": f"{ORIGIN}/mcp/p/bob",
        },
    )
    assert r.json()["error"] == "invalid_target"


def test_confidential_client_needs_its_secret(client: TestClient, parent: str) -> None:
    reg = register(client, token_endpoint_auth_method="client_secret_basic")
    assert reg["client_secret"]
    verifier, challenge = pkce()
    form = open_consent(client, reg["client_id"], challenge)
    code = query(approve(client, form, parent, ["read"]))["code"]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
    }
    r = client.post("/oauth/token", data={**body, "client_id": reg["client_id"]})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    basic = base64.b64encode(f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
    r = client.post("/oauth/token", data=body, headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text


# ---- consent page ------------------------------------------------------------------------


def test_consent_rejections(client: TestClient, store: Store, parent: str) -> None:
    reg = register(client)
    _, challenge = pkce()
    form = open_consent(client, reg["client_id"], challenge)

    r = approve(client, form, "akd_00000000_wrong", ["read"])
    assert r.status_code == 400 and "not a valid Ankido token" in r.text
    bob, _ = store.create_token(profile="bob", scopes=frozenset({"read"}))
    r = approve(client, form, bob, ["read"])
    assert r.status_code == 400 and "not a valid Ankido token" in r.text
    r = approve(client, form, parent, ["read", "sync"])
    assert r.status_code == 400 and "cannot grant: sync" in r.text
    r = approve(client, form, parent, [])
    assert r.status_code == 400 and "at least one scope" in r.text

    bad = {**form, "csrf": "forged"}
    r = approve(client, bad, parent, ["read"])
    assert r.status_code == 400 and "could not be verified" in r.text

    q = query(approve(client, form, "", [], action="deny"))
    assert q["error"] == "access_denied" and q["state"] == "st4te"
    r = approve(client, form, parent, ["read"])
    assert r.status_code == 400 and "expired" in r.text

    outcomes = [a["outcome"] for a in store.audit_tail(20) if a["action"] == "oauth_consent"]
    assert outcomes.count("bad_token") == 2


def test_admin_token_can_grant_everything_but_admin(client: TestClient, store: Store) -> None:
    root, _ = store.create_token(profile=None, scopes=frozenset({"admin"}))
    reg = register(client)
    verifier, challenge = pkce()
    form = open_consent(client, reg["client_id"], challenge, scope="read sync admin")
    page_scopes = ["read", "add", "review", "sync"]
    code = query(approve(client, form, root, page_scopes))["code"]
    tok = exchange(client, reg, code, verifier).json()
    assert tok["scope"] == "read add review sync"


def test_authorize_errors(client: TestClient) -> None:
    reg = register(client)
    _, challenge = pkce()
    r = client.get("/oauth/authorize", params={"client_id": "akdcl_unknown"})
    assert r.status_code == 400 and "Unknown or invalid client" in r.text
    r = client.get(
        "/oauth/authorize",
        params={"client_id": reg["client_id"], "redirect_uri": "https://evil.example/cb"},
    )
    assert r.status_code == 400 and "not registered" in r.text
    base = {
        "client_id": reg["client_id"],
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "state": "s",
    }
    q = query(client.get("/oauth/authorize", params=base))
    assert q["error"] == "invalid_request"
    q = query(
        client.get(
            "/oauth/authorize",
            params={
                **base,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": f"{ORIGIN}/mcp/p/nobody",
            },
        )
    )
    assert q["error"] == "invalid_target"


def test_loopback_redirect_any_port(client: TestClient, parent: str) -> None:
    reg = register(client, redirect_uris=["http://127.0.0.1/callback"])
    _, challenge = pkce()
    form = open_consent(
        client, reg["client_id"], challenge, redirect_uri="http://127.0.0.1:53817/callback"
    )
    q = approve(client, form, parent, ["read"])
    assert q.headers["location"].startswith("http://127.0.0.1:53817/callback?code=")


def test_register_validation(client: TestClient) -> None:
    for body in (
        {"redirect_uris": []},
        {"redirect_uris": ["http://evil.example/cb"]},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "private_key_jwt"},
        {"redirect_uris": [CALLBACK], "grant_types": ["client_credentials"]},
    ):
        r = client.post("/oauth/register", json=body)
        assert r.status_code == 400, body
        assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")
    reg = register(client, redirect_uris=["cursor://anysphere.cursor-mcp/oauth/callback"])
    assert reg["client_id"].startswith("akdcl_")


# ---- metadata-document clients -----------------------------------------------------------


def test_client_id_metadata_document(app: FastAPI, client: TestClient, parent: str) -> None:
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return json.dumps(
            {"client_id": CIMD_URL, "client_name": "Doc Client", "redirect_uris": [CALLBACK]}
        ).encode()

    app.state.oauth_clients = ClientRegistry(app.state.supervisor.store, fetcher=fetch)
    verifier, challenge = pkce()
    form = open_consent(client, CIMD_URL, challenge)
    code = query(approve(client, form, parent, ["read"]))["code"]
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": CIMD_URL,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    assert fetched == [CIMD_URL]  # cached after the first fetch


@pytest.mark.parametrize(
    "doc",
    [
        {"client_id": "https://other.example/c.json", "redirect_uris": [CALLBACK]},
        {"client_id": CIMD_URL},
        {"client_id": CIMD_URL, "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "x"},
    ],
)
def test_bad_metadata_documents(store: Store, doc: dict[str, Any]) -> None:
    registry = ClientRegistry(store, fetcher=lambda url: json.dumps(doc).encode())
    with pytest.raises(OAuthError):
        registry.get(CIMD_URL)


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1/c.json", "https://[::1]/c.json", "https://localhost/c.json"],
)
def test_metadata_fetch_refuses_private_addresses(url: str) -> None:
    with pytest.raises(OAuthError):
        oauth.fetch_metadata_document(url)


# ---- store ------------------------------------------------------------------------------


def test_expired_access_token_is_refused(store: Store) -> None:
    parent, _ = store.create_token(profile="alice", scopes=frozenset({"read"}))
    issued = store.create_oauth_grant(
        profile="alice",
        scopes=frozenset({"read"}),
        name="oauth:test",
        parent_id=parent.split("_")[1],
        client_id="c",
        resource=RESOURCE,
        access_ttl=0,
        expires_at=int(time.time()) + 60,
    )
    assert store.verify_oauth_access(issued.access_token) is None


def test_old_database_gets_new_token_columns(tmp_path: Any) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tokens (id TEXT PRIMARY KEY, profile TEXT, name TEXT NOT NULL DEFAULT '',"
        " hash TEXT NOT NULL, scopes TEXT NOT NULL, created_at INTEGER NOT NULL,"
        " expires_at INTEGER, revoked_at INTEGER, last_used_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO tokens VALUES ('abcd1234', 'alice', '', 'h', 'read', 0, NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()
    s = Store(path)
    rec = s.get_token("abcd1234")
    assert rec is not None and rec.kind == "static" and rec.parent_id is None
    s.close()


def test_consent_page_is_rate_limited(make_config: ConfigMaker) -> None:
    app = create_app(
        make_config(
            server={"public_url": ORIGIN, "rate_limits": {"oauth": {"per_minute": 1, "burst": 2}}}
        )
    )
    with TestClient(app, base_url=ORIGIN) as c:
        codes = [
            c.get("/oauth/authorize", params={"client_id": "akdcl_x"}).status_code for _ in range(3)
        ]
    assert codes == [400, 400, 429]


def test_consent_redirect_is_303(client: TestClient, parent: str) -> None:
    reg = register(client)
    _, challenge = pkce()
    form = open_consent(client, reg["client_id"], challenge)
    assert approve(client, form, parent, ["read"]).status_code == 303


def test_revoke_ignores_other_clients_tokens(client: TestClient, parent: str) -> None:
    reg, code, verifier = authorize(client, parent)
    tok = exchange(client, reg, code, verifier).json()
    other = register(client)
    r = client.post(
        "/oauth/revoke", data={"token": tok["access_token"], "client_id": other["client_id"]}
    )
    assert r.status_code == 200
    assert mcp_status(client, tok["access_token"]) == 200


def test_grant_dies_with_parent_even_without_cascade(store: Store) -> None:
    _, rec = store.create_token(profile="alice", scopes=frozenset({"read"}))
    issued = store.create_oauth_grant(
        profile="alice",
        scopes=frozenset({"read"}),
        name="oauth:test",
        parent_id=rec.id,
        client_id="c",
        resource=RESOURCE,
        access_ttl=3600,
        expires_at=int(time.time()) + 60,
    )
    assert store.verify_oauth_access(issued.access_token) is not None
    # What a revoke racing the grant's creation would leave behind: parent revoked, child not.
    store.set_token_expiry(rec.id, int(time.time()) - 1)
    assert store.verify_oauth_access(issued.access_token) is None
    assert store.rotate_oauth_refresh(issued.refresh_token, "c", 3600) is None


def test_failed_metadata_lookups_are_cached(store: Store) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        raise OAuthError("invalid_client", "down")

    registry = ClientRegistry(store, fetcher=fetch)
    for _ in range(3):
        with pytest.raises(OAuthError):
            registry.get(CIMD_URL)
    assert calls == [CIMD_URL]


def test_metadata_fetch_refuses_other_ports() -> None:
    with pytest.raises(OAuthError, match="default https port"):
        oauth.fetch_metadata_document("https://client.example.org:8443/c.json")
