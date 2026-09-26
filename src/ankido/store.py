# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service state that is not part of any Anki collection.

One SQLite file (``<data_dir>/ankido.db``) holds:

* ``tokens`` — API tokens (argon2 hash only, never the secret),
* ``journal`` — client-supplied idempotency keys and the result they produced,
* ``audit`` — append-only log of who did what (no secrets, no card content).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SCOPES: tuple[str, ...] = ("read", "add", "review", "sync", "admin")
TOKEN_PREFIX = "akd"
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
"""


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
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (int(time.time()), token_id),
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
        if row is None:
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
    )
