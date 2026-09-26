# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owning an Anki collection: open with the schema gate, back up, sync, close.

A :class:`Session` is used by exactly one worker thread (invariant 2). It never performs a full
upload on its own (invariant 3) and refuses to open a collection that the library would upgrade
unless the profile allows it (invariant 4).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from anki.collection import Collection
from anki.errors import BackendError, SyncError, SyncErrorKind
from anki.sync_pb2 import SyncAuth, SyncCollectionResponse, SyncStatusResponse

from ankido.config import Credentials, ProfileConfig
from ankido.errors import (
    ApiError,
    ProfileUnavailable,
    SchemaUpgradeRequired,
    SyncRequiredFull,
    UpstreamError,
)
from ankido.logging import get_logger, register_secret

log = get_logger("ankido.session")

# Schema version the pinned `anki` library writes. A collection with a lower `ver` would be
# upgraded on open, which makes every other client of that account demand a one-off full sync.
LIB_SCHEMA_VERSION = 18
_MEDIA_WAIT_SECONDS = 180.0


class SyncOutcome(str, Enum):
    NO_CHANGES = "no_changes"
    MERGED = "merged"
    DOWNLOADED = "downloaded"
    UPLOADED = "uploaded"


@dataclass
class SyncResult:
    outcome: SyncOutcome
    local_replaced: bool
    server_message: str
    media: str  # "synced" | "skipped" | "in_progress" | "failed: ..."
    duration_ms: int
    host_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "local_replaced": self.local_replaced,
            "server_message": self.server_message,
            "media": self.media,
            "duration_ms": self.duration_ms,
        }


def inspect_collection_file(path: Path) -> dict[str, Any] | None:
    """Read ``ver``/``scm``/counts from a collection with plain sqlite, without touching the lib."""
    if not path.is_file():
        return None
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            ver, scm = conn.execute("select ver, scm from col").fetchone()
            notes = conn.execute("select count() from notes").fetchone()[0]
            cards = conn.execute("select count() from cards").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ProfileUnavailable(f"collection file unreadable: {exc}") from exc
    return {"ver": int(ver), "scm": int(scm), "notes": int(notes), "cards": int(cards)}


class Session:
    def __init__(
        self,
        name: str,
        cfg: ProfileConfig,
        profile_dir: Path,
        credentials: Credentials | None,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self.profile_dir = profile_dir
        self.credentials = credentials
        self.col: Collection | None = None
        self.opened_at: float | None = None
        self.last_sync_at: float | None = None
        self.last_sync_result: SyncResult | None = None
        self.last_sync_error: str | None = None
        self.full_sync_pending: bool = False
        self.schema_upgraded: bool = False
        if credentials:
            register_secret(credentials.password)

    # ---- lifecycle ----------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.col is not None

    @property
    def path(self) -> Path:
        return self.cfg.collection

    def backups_dir(self) -> Path:
        return self.profile_dir / "backups"

    def open(self) -> Collection:
        if self.col is not None:
            return self.col
        info = inspect_collection_file(self.path)
        if info is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            log.info("creating new collection", fields={"profile": self.name})
        elif info["ver"] < LIB_SCHEMA_VERSION:
            if not self.cfg.allow_schema_upgrade:
                raise SchemaUpgradeRequired(
                    f"collection schema is v{info['ver']}, library needs v{LIB_SCHEMA_VERSION}; "
                    "opening would upgrade it and force a full sync on every other device. "
                    "Set allow_schema_upgrade: true for this profile to proceed "
                    "(a backup is taken first).",
                    details={"current": info["ver"], "required": LIB_SCHEMA_VERSION},
                )
            self.backup("pre-schema-upgrade")
            log.warning(
                "upgrading collection schema",
                fields={"profile": self.name, "from": info["ver"], "to": LIB_SCHEMA_VERSION},
            )
            self.schema_upgraded = True
        self.col = Collection(str(self.path))
        self.opened_at = time.time()
        return self.col

    def close(self) -> None:
        if self.col is None:
            return
        try:
            self.col.close(downgrade=False)
        finally:
            self.col = None
            self.opened_at = None
            log.info("collection closed", fields={"profile": self.name})

    def require(self) -> Collection:
        return self.open()

    def backup(self, reason: str) -> Path:
        """Copy the collection file (with WAL checkpointed) into ``backups/`` with retention."""
        dest_dir = self.backups_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"{now % 1:.3f}"[1:]
        dest = dest_dir / f"collection-{stamp}-{reason}.anki2"
        was_open = self.col is not None
        if was_open:
            self.close()
        if self.path.is_file():
            src = sqlite3.connect(str(self.path))
            try:
                dst = sqlite3.connect(str(dest))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        else:
            dest.touch()
        if was_open:
            self.open()
        self._prune_backups()
        log.info("backup written", fields={"profile": self.name, "path": str(dest)})
        return dest

    def _prune_backups(self) -> None:
        files = sorted(self.backups_dir().glob("collection-*.anki2"))
        for old in files[: -self.cfg.backups.keep]:
            old.unlink(missing_ok=True)

    # ---- sync ---------------------------------------------------------------------------

    def _key_path(self) -> Path:
        return self.profile_dir / "sync.key"

    def _auth(self, col: Collection, *, force_login: bool = False) -> SyncAuth:
        if self.credentials is None:
            raise ApiError(
                "profile has no AnkiWeb credentials configured", code="sync_not_configured"
            )
        key_path = self._key_path()
        if not force_login and key_path.is_file():
            try:
                cached = json.loads(key_path.read_text(encoding="utf-8"))
            except ValueError:
                cached = {}
            hkey = str(cached.get("hkey") or "")
            if hkey:
                register_secret(hkey)
                auth = SyncAuth(hkey=hkey)
                endpoint = cached.get("endpoint") or self.cfg.sync_endpoint
                if endpoint:
                    auth.endpoint = str(endpoint)
                return auth
        try:
            auth = col.sync_login(
                username=self.credentials.username,
                password=self.credentials.password,
                endpoint=self.cfg.sync_endpoint,
            )
        except BackendError as exc:
            raise _map_sync_error(exc) from exc
        register_secret(auth.hkey)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        payload = {"hkey": auth.hkey, "endpoint": auth.endpoint or None}
        key_path.write_text(json.dumps(payload), encoding="utf-8")
        key_path.chmod(0o600)
        return auth

    def sync_status(self) -> dict[str, Any]:
        col = self.require()
        out: dict[str, Any] = {
            "last_sync_at": self.last_sync_at,
            "last_result": self.last_sync_result.to_dict() if self.last_sync_result else None,
            "last_error": self.last_sync_error,
            "full_sync_pending": self.full_sync_pending,
            "configured": self.credentials is not None,
        }
        if self.credentials is None:
            return out
        try:
            status = col.sync_status(self._auth(col))
        except ApiError as exc:
            out["remote"] = {"error": exc.code}
            return out
        required = SyncStatusResponse.Required.Name(status.required).lower()
        out["remote"] = {"required": required, "has_changes": status.required != 0}
        self.full_sync_pending = status.required == SyncStatusResponse.FULL_SYNC
        out["full_sync_pending"] = self.full_sync_pending
        return out

    def sync(self, *, force_full: str | None = None) -> SyncResult:
        """Incremental sync. ``force_full`` in {"upload","download"} is the admin escape hatch."""
        col = self.require()
        started = time.monotonic()
        key_was_cached = self._key_path().is_file()
        auth = self._auth(col)
        usn_before = self._usn(col)
        try:
            result = col.sync_collection(auth, sync_media=self.cfg.media_sync)
        except BackendError as exc:
            if _is_auth_error(exc) and key_was_cached:
                # Cached sync key rejected (password changed, key revoked): log in again once.
                self._key_path().unlink()
                auth = self._auth(col, force_login=True)
                try:
                    result = col.sync_collection(auth, sync_media=self.cfg.media_sync)
                except BackendError as exc2:
                    self._record_error(exc2)
                    raise _map_sync_error(exc2) from exc2
            else:
                self._record_error(exc)
                raise _map_sync_error(exc) from exc
        if result.new_endpoint:
            auth.endpoint = result.new_endpoint
        required = result.required
        local_replaced = False
        outcome: SyncOutcome
        normal_done = required in (
            SyncCollectionResponse.NO_CHANGES,
            SyncCollectionResponse.NORMAL_SYNC,
        )
        if force_full is not None:
            # Admin escape hatch: do the full sync whether or not the server demanded one.
            upload = force_full == "upload"
            self._full(
                col, auth, result.server_media_usn, upload=upload, reason=f"force-{force_full}"
            )
            outcome = SyncOutcome.UPLOADED if upload else SyncOutcome.DOWNLOADED
            local_replaced = not upload
        elif normal_done:
            # The library performs a normal sync inside sync_collection and reports NO_CHANGES
            # when it is done; the USN moves only if something was actually exchanged.
            outcome = SyncOutcome.MERGED if self._usn(col) != usn_before else SyncOutcome.NO_CHANGES
        elif required == SyncCollectionResponse.FULL_DOWNLOAD or (
            required == SyncCollectionResponse.FULL_SYNC and self._is_empty(col)
        ):
            # Nothing local to lose: fetching the remote copy is the only sane step.
            self._full(col, auth, result.server_media_usn, upload=False, reason="bootstrap")
            outcome = SyncOutcome.DOWNLOADED
            local_replaced = True
        elif required in (SyncCollectionResponse.FULL_SYNC, SyncCollectionResponse.FULL_UPLOAD):
            self.full_sync_pending = True
            which = "full_upload" if required == SyncCollectionResponse.FULL_UPLOAD else "full"
            raise SyncRequiredFull(
                "AnkiWeb requires a full sync; refusing to do it implicitly. "
                "Use the admin force_full call after taking a backup.",
                details={"required": which, "server_message": result.server_message},
            )
        else:  # pragma: no cover - future enum values
            raise UpstreamError(f"unexpected sync requirement {required}")
        self.full_sync_pending = False
        if self.cfg.media_sync and outcome in (SyncOutcome.DOWNLOADED, SyncOutcome.UPLOADED):
            # A full sync aborts any media sync that was running; start a fresh one.
            col = self.require()
            col.sync_media(auth)
        media = self._wait_media(self.require()) if self.cfg.media_sync else "skipped"
        self.last_sync_at = time.time()
        self.last_sync_error = None
        self.last_sync_result = SyncResult(
            outcome=outcome,
            local_replaced=local_replaced,
            server_message=result.server_message,
            media=media,
            duration_ms=int((time.monotonic() - started) * 1000),
            host_number=result.host_number,
        )
        return self.last_sync_result

    def _full(
        self, col: Collection, auth: SyncAuth, server_usn: int, *, upload: bool, reason: str
    ) -> None:
        if not self._is_empty(col):
            self.backup(f"pre-{reason}")
            col = self.require()
        log.warning(
            "full sync",
            fields={"profile": self.name, "direction": "upload" if upload else "download"},
        )
        col.close_for_full_sync()
        try:
            col.full_upload_or_download(auth=auth, server_usn=server_usn, upload=upload)
        except BackendError as exc:
            self._record_error(exc)
            col.reopen(after_full_sync=False)
            raise _map_sync_error(exc) from exc
        col.reopen(after_full_sync=True)

    @staticmethod
    def _is_empty(col: Collection) -> bool:
        assert col.db is not None
        return (
            int(col.db.scalar("select count() from notes") or 0) == 0
            and int(col.db.scalar("select count() from cards") or 0) == 0
        )

    @staticmethod
    def _usn(col: Collection) -> int:
        assert col.db is not None
        return int(col.db.scalar("select usn from col") or 0)

    def _wait_media(self, col: Collection) -> str:
        deadline = time.monotonic() + _MEDIA_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                status = col.media_sync_status()
            except Exception as exc:  # the lib raises the media sync's own error here
                return f"failed: {type(exc).__name__}"
            if not status.active:
                return "synced"
            time.sleep(0.25)
        return "in_progress"

    def _record_error(self, exc: Exception) -> None:
        self.last_sync_error = type(exc).__name__


def _is_auth_error(exc: BackendError) -> bool:
    return isinstance(exc, SyncError) and exc.kind == SyncErrorKind.AUTH


def _map_sync_error(exc: BackendError) -> ApiError:
    if _is_auth_error(exc):
        return ApiError(
            "AnkiWeb rejected the credentials", code="sync_auth_failed", status=502, retryable=False
        )
    if isinstance(exc, SyncError):
        return UpstreamError(f"AnkiWeb sync failed: {exc}", code="sync_failed")
    return ApiError(f"sync failed: {exc}", code="sync_failed", status=502, retryable=True)
