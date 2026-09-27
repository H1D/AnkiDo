# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service state that is not part of any Anki collection.

One SQLite file (``<data_dir>/ankido.db``) holds:

* ``tokens`` — API tokens (argon2 hash only, never the secret),
* ``journal`` — client-supplied idempotency keys and the result they produced,
* ``audit`` — append-only log of who did what (no secrets, no card content),
* ``oauth_*`` — OAuth clients, authorization codes and the secrets of OAuth grants. A grant is a
  ``tokens`` row of kind ``oauth`` (so it shows up in ``token list`` and can be revoked there);
  its rotating access/refresh secrets live in ``oauth_grants`` as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SCOPES: tuple[str, ...] = ("read", "add", "review", "sync", "admin")
TOKEN_PREFIX = "akd"
ACCESS_PREFIX = "akda"
REFRESH_PREFIX = "akdr"
CODE_PREFIX = "akdc"
JOURNAL_TTL_SECONDS = 90 * 86400  # spec: ≥30 days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id          TEXT PRIMARY KEY,
    profile     TEXT,                 -- NULL = global admin token
    name        TEXT NOT NULL DEFAULT '',
    hash        TEXT NOT NULL,
    scopes      TEXT NOT NULL,        -- comma separated
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER,
    revoked_at  INTEGER,
    last_used_at INTEGER
);
CREATE TABLE IF NOT EXISTS journal (
    profile     TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- 'review' | 'note'
    client_id   TEXT NOT NULL,
    result      TEXT NOT NULL,        -- JSON
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (profile, kind, client_id)
);
CREATE INDEX IF NOT EXISTS journal_created ON journal(created_at);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    token_id    TEXT,
    profile     TEXT,
    action      TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    outcome     TEXT NOT NULL,
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,       -- 'dcr' | 'cimd'
    name         TEXT NOT NULL DEFAULT '',
    redirect_uris TEXT NOT NULL,      -- JSON list
    auth_method  TEXT NOT NULL,       -- 'none' | 'client_secret_post' | 'client_secret_basic'
    secret_hash  TEXT,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash    TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    profile      TEXT NOT NULL,
    scopes       TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    challenge    TEXT NOT NULL,
    resource     TEXT NOT NULL,
    parent_id    TEXT NOT NULL,
    expires_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_grants (
    token_id          TEXT PRIMARY KEY,   -- tokens.id
    client_id         TEXT NOT NULL,
    resource          TEXT NOT NULL,
    access_hash       TEXT NOT NULL,
    access_expires_at INTEGER NOT NULL,
    prev_access_hash  TEXT,
    prev_access_expires_at INTEGER,
    refresh_hash      TEXT NOT NULL
);
"""

# Columns added after 0.1; ALTER-ed into existing databases on open.
_TOKEN_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'static'",  # 'static' | 'oauth'
    "parent_id": "TEXT",  # oauth grants: the token pasted on the consent page
}


@dataclass(frozen=True)
class TokenRecord:
    id: str
    profile: str | None
    name: str
    scopes: frozenset[str]
    created_at: int
    expires_at: int | None
    revoked_at: int | None
    last_used_at: int | None
    kind: str = "static"
    parent_id: str | None = None
    audience: str | None = None  # set only when authenticated with an OAuth access token

    @property
    def is_admin_global(self) -> bool:
        return self.profile is None

    def is_valid(self, now: int | None = None) -> bool:
        now = now or int(time.time())
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= now)

    def allows(self, profile: str, scope: str) -> bool:
        if not self.is_valid():
            return False
        if self.profile is not None and self.profile != profile:
            return False
        if "admin" in self.scopes:
            return True
        return scope in self.scopes


def parse_scopes(text: str) -> frozenset[str]:
    scopes = frozenset(s.strip() for s in text.split(",") if s.strip())
    bad = scopes - set(SCOPES)
    if bad:
        raise ValueError(f"unknown scope(s): {', '.join(sorted(bad))}; valid: {', '.join(SCOPES)}")
    if not scopes:
        raise ValueError("at least one scope is required")
    return scopes


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(tokens)")}
        for column, decl in _TOKEN_COLUMNS.items():
            if column not in have:
                self._conn.execute(f"ALTER TABLE tokens ADD COLUMN {column} {decl}")
        self._hasher = PasswordHasher()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- tokens -------------------------------------------------------------------------

    def create_token(
        self,
        *,
        profile: str | None,
        scopes: frozenset[str],
        name: str = "",
        expires_in_seconds: int | None = None,
    ) -> tuple[str, TokenRecord]:
        """Create a token; returns ``(secret, record)``. The secret is shown exactly once."""
        token_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        raw = f"{TOKEN_PREFIX}_{token_id}_{secret}"
        now = int(time.time())
        expires_at = now + expires_in_seconds if expires_in_seconds else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens(id, profile, name, hash, scopes, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    token_id,
                    profile,
                    name,
                    self._hasher.hash(secret),
                    ",".join(sorted(scopes)),
                    now,
                    expires_at,
                ),
            )
        rec = self.get_token(token_id)
        assert rec is not None
        return raw, rec

    def get_token(self, token_id: str) -> TokenRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
        return _row_to_token(row) if row else None

    def list_tokens(self, profile: str | None = None) -> list[TokenRecord]:
        with self._lock:
            if profile is None:
                rows = self._conn.execute("SELECT * FROM tokens ORDER BY created_at").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tokens WHERE profile=? ORDER BY created_at", (profile,)
                ).fetchall()
        return [_row_to_token(r) for r in rows]

    def revoke_token(self, token_id: str) -> bool:
        """Revoke a token and every OAuth grant that was approved with it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tokens SET revoked_at=? WHERE (id=? OR parent_id=?) AND revoked_at IS NULL",
                (int(time.time()), token_id, token_id),
            )
        return cur.rowcount > 0

    def verify_token(self, raw: str) -> TokenRecord | None:
        """Return the record if ``raw`` is a valid, unexpired, unrevoked token."""
        parts = raw.split("_", 2)
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            return None
        _, token_id, secret = parts
        with self._lock:
            row = self._conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
        if row is None or row["kind"] != "static":
            return None
        try:
            self._hasher.verify(row["hash"], secret)
        except VerifyMismatchError:
            return None
        rec = _row_to_token(row)
        if not rec.is_valid():
            return None
        return rec

    def touch_token(self, token_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tokens SET last_used_at=? WHERE id=?", (int(time.time()), token_id)
            )

    # ---- idempotency journal ------------------------------------------------------------

    def journal_get(self, profile: str, kind: str, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM journal WHERE profile=? AND kind=? AND client_id=?",
                (profile, kind, client_id),
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def journal_get_many(
        self, profile: str, kind: str, client_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not client_ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for i in range(0, len(client_ids), 500):
                chunk = client_ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT client_id, result FROM journal WHERE profile=? AND kind=?"
                    f" AND client_id IN ({marks})",
                    (profile, kind, *chunk),
                ).fetchall()
                for r in rows:
                    out[r["client_id"]] = json.loads(r["result"])
        return out

    def journal_put(self, profile: str, kind: str, client_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO journal(profile, kind, client_id, result, created_at)"
                " VALUES (?,?,?,?,?)",
                (profile, kind, client_id, json.dumps(result), int(time.time())),
            )

    def journal_prune(self, ttl_seconds: int = JOURNAL_TTL_SECONDS) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM journal WHERE created_at < ?", (int(time.time()) - ttl_seconds,)
            )
        return cur.rowcount

    # ---- audit log ----------------------------------------------------------------------

    def audit(
        self,
        *,
        token_id: str | None,
        profile: str | None,
        action: str,
        outcome: str,
        count: int = 0,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(ts, token_id, profile, action, count, outcome, detail)"
                " VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), token_id, profile, action, count, outcome, detail),
            )

    def audit_tail(self, limit: int = 100, profile: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if profile is None:
                rows = self._conn.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit WHERE profile=? ORDER BY id DESC LIMIT ?",
                    (profile, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    # ---- meta ---------------------------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))

    # ---- OAuth --------------------------------------------------------------------------

    def put_oauth_client(
        self,
        *,
        client_id: str,
        kind: str,
        name: str,
        redirect_uris: list[str],
        auth_method: str,
        secret: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_clients(client_id, kind, name, redirect_uris, auth_method,"
                " secret_hash, created_at, last_used_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(client_id) DO UPDATE SET name=excluded.name,"
                " redirect_uris=excluded.redirect_uris, auth_method=excluded.auth_method,"
                " last_used_at=excluded.last_used_at",
                (
                    client_id,
                    kind,
                    name,
                    json.dumps(redirect_uris),
                    auth_method,
                    _sha256(secret) if secret else None,
                    now,
                    now,
                ),
            )

    def get_oauth_client(self, client_id: str) -> OAuthClient | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        return OAuthClient(
            client_id=row["client_id"],
            kind=row["kind"],
            name=row["name"],
            redirect_uris=json.loads(row["redirect_uris"]),
            auth_method=row["auth_method"],
            secret_hash=row["secret_hash"],
            last_used_at=row["last_used_at"],
        )

    def touch_oauth_client(self, client_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE oauth_clients SET last_used_at=? WHERE client_id=?",
                (int(time.time()), client_id),
            )

    def prune_oauth(self, client_ttl_seconds: int) -> int:
        """Drop expired codes and clients unused for ``client_ttl_seconds``."""
        now = int(time.time())
        with self._lock:
            self._conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            cur = self._conn.execute(
                "DELETE FROM oauth_clients WHERE last_used_at < ?", (now - client_ttl_seconds,)
            )
        return cur.rowcount

    def put_oauth_code(
        self,
        *,
        client_id: str,
        profile: str,
        scopes: frozenset[str],
        redirect_uri: str,
        challenge: str,
        resource: str,
        parent_id: str,
        ttl_seconds: int,
    ) -> str:
        raw = f"{CODE_PREFIX}_{secrets.token_urlsafe(32)}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_codes(code_hash, client_id, profile, scopes, redirect_uri,"
                " challenge, resource, parent_id, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _sha256(raw),
                    client_id,
                    profile,
                    ",".join(sorted(scopes)),
                    redirect_uri,
                    challenge,
                    resource,
                    parent_id,
                    int(time.time()) + ttl_seconds,
                ),
            )
        return raw

    def take_oauth_code(self, raw: str) -> OAuthCode | None:
        """Consume a code (single use). Returns ``None`` if unknown or expired."""
        with self._lock:
            row = self._conn.execute(
                "DELETE FROM oauth_codes WHERE code_hash=? RETURNING *", (_sha256(raw),)
            ).fetchone()
        if row is None or row["expires_at"] < int(time.time()):
            return None
        return OAuthCode(
            client_id=row["client_id"],
            profile=row["profile"],
            scopes=frozenset(row["scopes"].split(",")),
            redirect_uri=row["redirect_uri"],
            challenge=row["challenge"],
            resource=row["resource"],
            parent_id=row["parent_id"],
        )

    def create_oauth_grant(
        self,
        *,
        profile: str,
        scopes: frozenset[str],
        name: str,
        parent_id: str,
        client_id: str,
        resource: str,
        access_ttl: int,
        expires_at: int,
    ) -> IssuedTokens:
        """Create a grant (a ``tokens`` row of kind ``oauth``) and its first token pair."""
        token_id = secrets.token_hex(4)
        now = int(time.time())
        access, refresh = _new_pair(token_id)
        access_expires = min(now + access_ttl, expires_at)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens(id, profile, name, hash, scopes, created_at, expires_at,"
                " kind, parent_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    profile,
                    name,
                    "",
                    ",".join(sorted(scopes)),
                    now,
                    expires_at,
                    "oauth",
                    parent_id,
                ),
            )
            self._conn.execute(
                "INSERT INTO oauth_grants(token_id, client_id, resource, access_hash,"
                " access_expires_at, refresh_hash) VALUES (?,?,?,?,?,?)",
                (token_id, client_id, resource, _sha256(access), access_expires, _sha256(refresh)),
            )
        rec = self.get_token(token_id)
        assert rec is not None
        return IssuedTokens(access, refresh, access_expires - now, rec, resource)

    def verify_oauth_access(self, raw: str) -> TokenRecord | None:
        """The grant behind a current (or just-rotated, still unexpired) access token."""
        token_id = _grant_id(raw, ACCESS_PREFIX)
        if token_id is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_grants WHERE token_id=?", (token_id,)
            ).fetchone()
        if row is None:
            return None
        now = int(time.time())
        digest = _sha256(raw)
        current = hmac.compare_digest(digest, row["access_hash"]) and row["access_expires_at"] > now
        previous = (
            row["prev_access_hash"] is not None
            and hmac.compare_digest(digest, row["prev_access_hash"])
            and (row["prev_access_expires_at"] or 0) > now
        )
        if not (current or previous):
            return None
        rec = self.get_token(token_id)
        if rec is None or not rec.is_valid() or not self._parent_valid(rec):
            return None
        return _with_audience(rec, row["resource"])

    def _parent_valid(self, rec: TokenRecord) -> bool:
        # Revocation cascades, but a grant created while its parent was being revoked could
        # miss the cascade; checking here closes that window.
        if rec.parent_id is None:
            return True
        parent = self.get_token(rec.parent_id)
        return parent is not None and parent.is_valid()

    def rotate_oauth_refresh(
        self, raw: str, client_id: str, access_ttl: int
    ) -> IssuedTokens | None:
        """Exchange a refresh token for a new pair; the old refresh token dies immediately.

        The access token it replaces keeps working until it expires, so requests already in
        flight when a client refreshes do not fail.
        """
        token_id = _grant_id(raw, REFRESH_PREFIX)
        if token_id is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_grants WHERE token_id=?", (token_id,)
            ).fetchone()
            if (
                row is None
                or row["client_id"] != client_id
                or not hmac.compare_digest(_sha256(raw), row["refresh_hash"])
            ):
                return None
            rec = self.get_token(token_id)
            if rec is None or not rec.is_valid() or not self._parent_valid(rec):
                return None
            now = int(time.time())
            access, refresh = _new_pair(token_id)
            access_expires = min(now + access_ttl, rec.expires_at or now + access_ttl)
            self._conn.execute(
                "UPDATE oauth_grants SET prev_access_hash=access_hash,"
                " prev_access_expires_at=access_expires_at, access_hash=?, access_expires_at=?,"
                " refresh_hash=? WHERE token_id=?",
                (_sha256(access), access_expires, _sha256(refresh), token_id),
            )
        return IssuedTokens(access, refresh, access_expires - now, rec, row["resource"])

    def revoke_oauth_secret(self, raw: str, client_id: str) -> str | None:
        """Revoke the grant behind an access or refresh token issued to ``client_id``; returns
        its id if one matched (RFC 7009: other clients' tokens are ignored)."""
        for prefix, column in ((ACCESS_PREFIX, "access_hash"), (REFRESH_PREFIX, "refresh_hash")):
            token_id = _grant_id(raw, prefix)
            if token_id is None:
                continue
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM oauth_grants WHERE token_id=?", (token_id,)
                ).fetchone()
            if row is None or row["client_id"] != client_id:
                return None
            digest = _sha256(raw)
            if hmac.compare_digest(digest, row[column]) or (
                column == "access_hash"
                and row["prev_access_hash"] is not None
                and hmac.compare_digest(digest, row["prev_access_hash"])
            ):
                self.revoke_token(token_id)
                return token_id
        return None

    def set_token_expiry(self, token_id: str, expires_at: int | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE tokens SET expires_at=? WHERE id=?", (expires_at, token_id))

    def oauth_grant_info(self, token_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT client_id, resource FROM oauth_grants WHERE token_id=?", (token_id,)
            ).fetchone()
        return dict(row) if row else None


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    kind: str
    name: str
    redirect_uris: list[str]
    auth_method: str
    secret_hash: str | None
    last_used_at: int

    def check_secret(self, secret: str | None) -> bool:
        if self.secret_hash is None:
            return True
        return secret is not None and hmac.compare_digest(_sha256(secret), self.secret_hash)


@dataclass(frozen=True)
class OAuthCode:
    client_id: str
    profile: str
    scopes: frozenset[str]
    redirect_uri: str
    challenge: str
    resource: str
    parent_id: str


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    record: TokenRecord
    resource: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_pair(token_id: str) -> tuple[str, str]:
    return (
        f"{ACCESS_PREFIX}_{token_id}_{secrets.token_urlsafe(32)}",
        f"{REFRESH_PREFIX}_{token_id}_{secrets.token_urlsafe(32)}",
    )


def _grant_id(raw: str, prefix: str) -> str | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != prefix:
        return None
    return parts[1]


def _with_audience(rec: TokenRecord, audience: str) -> TokenRecord:
    return replace(rec, audience=audience)


def _row_to_token(row: sqlite3.Row) -> TokenRecord:
    return TokenRecord(
        id=row["id"],
        profile=row["profile"],
        name=row["name"],
        scopes=frozenset(row["scopes"].split(",")),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
        kind=row["kind"],
        parent_id=row["parent_id"],
    )
