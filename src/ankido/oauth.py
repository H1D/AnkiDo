# SPDX-License-Identifier: AGPL-3.0-or-later
"""A minimal OAuth 2.1 authorization server for the MCP endpoints.

Ankido has no user accounts, so the person approving a client proves ownership of a profile by
pasting an existing Ankido token on the consent page. The grant can carry at most that token's
scopes (never ``admin``), is bound to one ``/mcp/p/{profile}`` resource, and dies with it.

Clients identify themselves with a Client ID Metadata Document (an https URL as ``client_id``)
or through Dynamic Client Registration. Everything here is framework-free; the HTTP surface is
in :mod:`ankido.api.oauth`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ankido.config import Config
from ankido.store import OAuthClient, Store, TokenRecord

OAUTH_SCOPES: tuple[str, ...] = ("read", "add", "review", "sync", "delete")
DEFAULT_SCOPES: tuple[str, ...] = ("read", "add", "review")
# Never ticked in advance on the consent page, even when a client asks for it.
OPT_IN_SCOPES: frozenset[str] = frozenset({"delete"})
ACCESS_TTL = 3600
REFRESH_TTL = 90 * 86400
CODE_TTL = 300
CLIENT_TTL = 90 * 86400  # DCR clients unused this long are forgotten
PENDING_TTL = 900  # how long a consent page stays valid
CIMD_CACHE_TTL = 3600
CIMD_FAILURE_TTL = 60  # failed lookups are remembered briefly so retries stay cheap
CIMD_CACHE_MAX = 256
CIMD_MAX_BYTES = 64 * 1024
CIMD_TIMEOUT = 5.0

_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_BLOCKED_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "about", "blob"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


class OAuthError(Exception):
    """An RFC 6749 error: ``{"error": …, "error_description": …}``."""

    def __init__(self, error: str, description: str, *, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def to_dict(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def oauth_status(config: Config) -> str:
    """``ok``, ``not_configured`` (no ``server.public_url``) or ``insecure`` (plain http)."""
    url = config.server.public_url
    if url is None:
        return "not_configured"
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in _LOOPBACK_HOSTS:
        return "insecure"
    return "ok"


def resource_url(public_url: str, profile: str) -> str:
    return f"{public_url}/mcp/p/{profile}"


def profile_from_resource(public_url: str, resource: str, profiles: dict[str, Any]) -> str | None:
    prefix = f"{public_url}/mcp/p/"
    if not resource.startswith(prefix):
        return None
    name = resource[len(prefix) :].rstrip("/")
    return name if name in profiles else None


def parse_scope(text: str | None) -> list[str]:
    """Known scopes from a space-separated ``scope`` parameter, in canonical order."""
    asked = set((text or "").split())
    return [s for s in OAUTH_SCOPES if s in asked]


def grantable_scopes(parent: TokenRecord) -> frozenset[str]:
    """What a pasted token may hand out: its own scopes, or all of them if it is an admin token."""
    if "admin" in parent.scopes:
        return frozenset(OAUTH_SCOPES)
    return frozenset(parent.scopes) & frozenset(OAUTH_SCOPES)


def verify_pkce(verifier: str, challenge: str) -> bool:
    if not _VERIFIER_RE.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


def _is_loopback_http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS | {"::1"}


def check_redirect_uri(uri: str) -> None:
    """Registration-time rules: https, http on loopback, or a native app's private scheme."""
    parts = urlsplit(uri)
    if not parts.scheme or parts.fragment:
        raise OAuthError("invalid_redirect_uri", f"redirect_uri {uri!r} must be absolute, no #")
    if parts.scheme.lower() in _BLOCKED_SCHEMES:
        raise OAuthError("invalid_redirect_uri", f"redirect_uri scheme {parts.scheme!r} refused")
    if parts.scheme == "http" and not _is_loopback_http(uri):
        raise OAuthError(
            "invalid_redirect_uri", "plain http redirect_uris are only allowed on localhost"
        )
    if parts.scheme in ("http", "https") and not parts.hostname:
        raise OAuthError("invalid_redirect_uri", f"redirect_uri {uri!r} has no host")


def redirect_uri_matches(registered: list[str], given: str) -> bool:
    """Exact match, except that loopback redirects may use any port (RFC 8252 §7.3)."""
    if given in registered:
        return True
    if not _is_loopback_http(given):
        return False
    g = urlsplit(given)
    for uri in registered:
        if not _is_loopback_http(uri):
            continue
        r = urlsplit(uri)
        if (r.hostname, r.path or "/", r.query) == (g.hostname, g.path or "/", g.query):
            return True
    return False


# ---- clients ----------------------------------------------------------------------------

Fetcher = Callable[[str], bytes]


def _resolve_public(host: str) -> str:
    """One public address for ``host``; refuses names that resolve to anything non-public."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OAuthError("invalid_client", f"client_id host {host!r} does not resolve") from exc
    addrs = [str(info[4][0]) for info in infos]
    if not addrs or not all(ipaddress.ip_address(a).is_global for a in addrs):
        raise OAuthError(
            "invalid_client", f"client_id host {host!r} resolves to a non-public address"
        )
    return addrs[0]


def fetch_metadata_document(url: str) -> bytes:
    """GET a client metadata document without becoming an SSRF proxy.

    https on port 443 only, public addresses only (the connection goes to the address that was
    checked, with the name kept for SNI and certificate checks), no redirects, no proxy or netrc
    from the environment, 5 s for the whole download, 64 KiB cap.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise OAuthError("invalid_client", "client_id host must be a name, not an IP")
    if parts.port not in (None, 443):
        raise OAuthError("invalid_client", "client_id URL must use the default https port")
    ip = _resolve_public(host)
    pinned = urlunsplit(
        ("https", f"[{ip}]" if ":" in ip else ip, parts.path or "/", parts.query, "")
    )
    deadline = time.monotonic() + CIMD_TIMEOUT
    try:
        with (
            httpx.Client(timeout=CIMD_TIMEOUT, follow_redirects=False, trust_env=False) as http,
            http.stream(
                "GET",
                pinned,
                headers={"Accept": "application/json", "Host": host},
                extensions={"sni_hostname": host},
            ) as resp,
        ):
            if resp.status_code != 200:
                raise OAuthError(
                    "invalid_client", f"client metadata document returned {resp.status_code}"
                )
            body = bytearray()
            for chunk in resp.iter_bytes():
                body.extend(chunk)
                if len(body) > CIMD_MAX_BYTES:
                    raise OAuthError("invalid_client", "client metadata document is too large")
                if time.monotonic() > deadline:
                    raise OAuthError("invalid_client", "client metadata document is too slow")
            return bytes(body)
    except httpx.HTTPError as exc:
        raise OAuthError("invalid_client", f"could not fetch client metadata: {exc}") from exc


def is_metadata_url(client_id: str) -> bool:
    parts = urlsplit(client_id)
    return parts.scheme == "https" and bool(parts.hostname) and parts.path not in ("", "/")


class ClientRegistry:
    """Looks up DCR clients in the store and resolves metadata-document clients over https."""

    def __init__(self, store: Store, fetcher: Fetcher = fetch_metadata_document) -> None:
        self._store = store
        self._fetch = fetcher
        self._cache: dict[str, tuple[float, OAuthClient | OAuthError]] = {}
        self._lock = threading.Lock()

    def get(self, client_id: str) -> OAuthClient:
        if is_metadata_url(client_id):
            return self._resolve_document(client_id)
        client = self._store.get_oauth_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown client_id; register again", status=401)
        return client

    def _resolve_document(self, url: str) -> OAuthClient:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(url)
        if hit and hit[0] > now:
            if isinstance(hit[1], OAuthError):
                raise hit[1]
            return hit[1]
        try:
            client = self._load_document(url)
        except OAuthError as exc:
            self._remember(url, now + CIMD_FAILURE_TTL, exc)
            raise
        self._remember(url, now + CIMD_CACHE_TTL, client)
        return client

    def _remember(self, url: str, until: float, value: OAuthClient | OAuthError) -> None:
        with self._lock:
            self._cache.pop(url, None)
            while len(self._cache) >= CIMD_CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            self._cache[url] = (until, value)

    def _load_document(self, url: str) -> OAuthClient:
        try:
            doc = json.loads(self._fetch(url))
        except ValueError as exc:
            raise OAuthError("invalid_client", "client metadata document is not JSON") from exc
        if not isinstance(doc, dict) or doc.get("client_id") != url:
            raise OAuthError(
                "invalid_client", "client metadata document's client_id must equal its URL"
            )
        uris = doc.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise OAuthError("invalid_client", "client metadata document needs redirect_uris")
        for u in uris:
            check_redirect_uri(u)
        method = doc.get("token_endpoint_auth_method", "none")
        if method != "none":
            raise OAuthError(
                "invalid_client",
                f"token_endpoint_auth_method {method!r} is not supported for metadata-document"
                " clients; use 'none'",
            )
        name = doc.get("client_name")
        client = OAuthClient(
            client_id=url,
            kind="cimd",
            name=name[:100] if isinstance(name, str) and name else urlsplit(url).netloc,
            redirect_uris=list(uris),
            auth_method="none",
            secret_hash=None,
            last_used_at=int(time.time()),
        )
        return client

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        """RFC 7591 dynamic registration. Returns the registration response."""
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty list")
        for u in uris:
            check_redirect_uri(u)
        method = body.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError(
                "invalid_client_metadata", f"token_endpoint_auth_method {method!r} not supported"
            )
        grant_types = body.get("grant_types", ["authorization_code", "refresh_token"])
        if not isinstance(grant_types, list) or not set(grant_types) <= {
            "authorization_code",
            "refresh_token",
        }:
            raise OAuthError("invalid_client_metadata", "unsupported grant_types")
        response_types = body.get("response_types", ["code"])
        if response_types != ["code"]:
            raise OAuthError("invalid_client_metadata", "only response_types ['code'] is supported")
        raw_name = body.get("client_name")
        name = raw_name.strip()[:100] if isinstance(raw_name, str) else ""
        client_id = "akdcl_" + secrets.token_urlsafe(16)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        self._store.prune_oauth(CLIENT_TTL)
        self._store.put_oauth_client(
            client_id=client_id,
            kind="dcr",
            name=name,
            redirect_uris=uris,
            auth_method=method,
            secret=secret,
        )
        out: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": uris,
            "token_endpoint_auth_method": method,
            "grant_types": grant_types,
            "response_types": ["code"],
        }
        if name:
            out["client_name"] = name
        if secret is not None:
            out["client_secret"] = secret
            out["client_secret_expires_at"] = 0
        return out


def client_label(client: OAuthClient) -> str:
    return client.name or (urlsplit(client.client_id).netloc if client.kind == "cimd" else "client")


# ---- pending consent --------------------------------------------------------------------


@dataclass
class PendingAuthorization:
    """An authorization request waiting on the consent page."""

    request_id: str
    csrf: str
    client: OAuthClient
    redirect_uri: str
    state: str | None
    challenge: str
    resource: str
    profile: str
    requested: list[str]
    expires_at: float


class PendingStore:
    """In memory: a restart just means the user clicks "connect" again."""

    def __init__(self) -> None:
        self._items: dict[str, PendingAuthorization] = {}
        self._lock = threading.Lock()

    def add(self, **kwargs: Any) -> PendingAuthorization:
        now = time.monotonic()
        item = PendingAuthorization(
            request_id=secrets.token_urlsafe(24),
            csrf=secrets.token_urlsafe(24),
            expires_at=now + PENDING_TTL,
            **kwargs,
        )
        with self._lock:
            for key in [k for k, v in self._items.items() if v.expires_at <= now]:
                del self._items[key]
            if len(self._items) >= 1000:  # bound memory against page-reload floods
                self._items.pop(next(iter(self._items)))
            self._items[item.request_id] = item
        return item

    def get(self, request_id: str) -> PendingAuthorization | None:
        with self._lock:
            item = self._items.get(request_id)
        if item is None or item.expires_at <= time.monotonic():
            return None
        return item

    def pop(self, request_id: str) -> None:
        with self._lock:
            self._items.pop(request_id, None)
