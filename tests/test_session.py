# SPDX-License-Identifier: AGPL-3.0-or-later
"""collection/session.py: open, schema gate and backup on real files; sync policy on a fake."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock

import pytest
from anki.errors import BackendError, SyncError, SyncErrorKind
from anki.sync_pb2 import SyncAuth, SyncCollectionResponse, SyncStatusResponse

from ankido.collection import session as session_mod
from ankido.collection.session import (
    LIB_SCHEMA_VERSION,
    Session,
    SyncOutcome,
    inspect_collection_file,
)
from ankido.config import Credentials, ProfileConfig
from ankido.errors import (
    ApiError,
    ProfileUnavailable,
    SchemaUpgradeRequired,
    SyncRequiredFull,
    UpstreamError,
)

# ---- real collection files ---------------------------------------------------------------


def make_session(
    tmp_path: Path,
    *,
    allow_upgrade: bool = False,
    keep: int = 10,
    creds: Credentials | None = None,
) -> Session:
    pdir = tmp_path / "prof"
    cfg = ProfileConfig(
        collection=pdir / "collection.anki2",
        allow_schema_upgrade=allow_upgrade,
        backups={"keep": keep},  # type: ignore[arg-type]
    )
    return Session("prof", cfg, pdir, creds)


def _set_ver(path: Path, ver: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("update col set ver=?", (ver,))
    conn.commit()
    conn.close()


def test_open_creates_collection_and_close(tmp_path: Path) -> None:
    s = make_session(tmp_path)
    assert not s.is_open and not s.path.exists()
    col = s.open()
    assert s.is_open and s.opened_at is not None and s.path.is_file()
    assert s.require() is col  # idempotent
    info = inspect_collection_file(s.path)
    assert info is not None
    assert info["ver"] == LIB_SCHEMA_VERSION and info["notes"] == 0 and info["cards"] == 0
    s.close()
    assert not s.is_open and s.opened_at is None
    s.close()  # idempotent
    assert not (s.path.parent / "collection.anki2-wal").exists()


def test_inspect_collection_file_missing_and_corrupt(tmp_path: Path) -> None:
    assert inspect_collection_file(tmp_path / "nope.anki2") is None
    bad = tmp_path / "bad.anki2"
    bad.write_bytes(b"this is not sqlite")
    with pytest.raises(ProfileUnavailable, match="unreadable"):
        inspect_collection_file(bad)


def test_schema_gate_refuses_old_schema_by_default(tmp_path: Path) -> None:
    s = make_session(tmp_path)
    s.open()
    s.close()
    _set_ver(s.path, 11)
    s2 = make_session(tmp_path)
    with pytest.raises(SchemaUpgradeRequired) as ei:
        s2.open()
    assert ei.value.status == 409 and ei.value.code == "schema_upgrade_required"
    assert ei.value.details == {"current": 11, "required": LIB_SCHEMA_VERSION}
    assert not s2.is_open and not s2.schema_upgraded
    assert not s2.backups_dir().exists()
    info = inspect_collection_file(s2.path)
    assert info is not None and info["ver"] == 11  # untouched


def test_schema_gate_upgrades_with_backup_when_allowed(tmp_path: Path) -> None:
    s = make_session(tmp_path)
    s.open()
    s.close()
    _set_ver(s.path, 17)
    s2 = make_session(tmp_path, allow_upgrade=True)
    try:
        s2.open()
    finally:
        s2.close()
    assert s2.schema_upgraded is True
    backups = list(s2.backups_dir().glob("collection-*-pre-schema-upgrade.anki2"))
    assert len(backups) == 1
    assert sqlite3.connect(str(backups[0])).execute("select ver from col").fetchone() == (17,)
    info = inspect_collection_file(s2.path)
    assert info is not None and info["ver"] == LIB_SCHEMA_VERSION


def test_backup_creates_file_and_prunes_to_keep(tmp_path: Path) -> None:
    s = make_session(tmp_path, keep=2)
    s.open()
    paths = [s.backup(reason) for reason in ("a", "b", "c")]
    assert s.is_open  # reopened after the backup
    assert all(p.parent == s.backups_dir() for p in paths)
    remaining = sorted(s.backups_dir().glob("collection-*.anki2"))
    assert len(remaining) == 2
    assert paths[-1] in remaining
    conn = sqlite3.connect(str(paths[-1]))
    assert conn.execute("select ver from col").fetchone() == (LIB_SCHEMA_VERSION,)
    conn.close()
    s.close()


def test_backup_without_collection_file_touches_placeholder(tmp_path: Path) -> None:
    s = make_session(tmp_path)
    p = s.backup("manual")
    assert p.is_file() and p.stat().st_size == 0 and not s.is_open


# ---- sync policy on a fake collection ----------------------------------------------------


class FakeDb:
    def __init__(self) -> None:
        self.usn = 1
        self.notes = 0
        self.cards = 0

    def scalar(self, sql: str, *args: Any) -> Any:
        if "usn" in sql:
            return self.usn
        if "notes" in sql:
            return self.notes
        if "cards" in sql:
            return self.cards
        return 0


def _resp(
    required: SyncCollectionResponse.ChangesRequired.ValueType, **kw: Any
) -> SyncCollectionResponse:
    return SyncCollectionResponse(required=required, **kw)


def _sync_error(kind: SyncErrorKind) -> SyncError:
    return SyncError("sync failed", None, None, None, kind)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Session, MagicMock, FakeDb]:
    col = MagicMock(name="Collection")
    db = FakeDb()
    col.db = db
    col.sync_login.return_value = SyncAuth(hkey="hkey-1", endpoint="")
    col.sync_collection.return_value = _resp(SyncCollectionResponse.NO_CHANGES)
    col.media_sync_status.return_value.active = False

    def fake_open(self: Session) -> Any:
        self.col = col
        return col

    monkeypatch.setattr(Session, "open", fake_open)
    monkeypatch.setattr(session_mod, "_MEDIA_WAIT_SECONDS", 0.6)
    pdir = tmp_path / "prof"
    cfg = ProfileConfig(collection=pdir / "collection.anki2")
    s = Session("prof", cfg, pdir, Credentials(username="u@example.com", password="pw-secret"))
    return s, col, db


Fake = tuple[Session, MagicMock, FakeDb]


def test_no_changes_when_usn_unchanged(fake: Fake, tmp_path: Path) -> None:
    s, col, _ = fake
    result = s.sync()
    assert result.outcome is SyncOutcome.NO_CHANGES
    assert result.local_replaced is False and result.media == "synced"
    assert result.to_dict()["outcome"] == "no_changes"
    assert s.last_sync_result is result and s.last_sync_at is not None
    assert s.last_sync_error is None and s.full_sync_pending is False
    col.full_upload_or_download.assert_not_called()
    col.sync_media.assert_not_called()
    col.sync_collection.assert_called_once()
    assert col.sync_collection.call_args.kwargs == {"sync_media": True}


def test_merged_when_usn_moved(fake: Fake) -> None:
    s, col, db = fake

    def do_sync(auth: SyncAuth, sync_media: bool) -> SyncCollectionResponse:
        db.usn += 3
        return _resp(SyncCollectionResponse.NORMAL_SYNC, server_message="hi", host_number=2)

    col.sync_collection.side_effect = do_sync
    result = s.sync()
    assert result.outcome is SyncOutcome.MERGED
    assert result.server_message == "hi" and result.host_number == 2


def test_sync_key_written_0600_and_reused(fake: Fake, tmp_path: Path) -> None:
    s, col, _ = fake
    s.sync()
    key = tmp_path / "prof" / "sync.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert json.loads(key.read_text()) == {"hkey": "hkey-1", "endpoint": None}
    s.sync()
    assert col.sync_login.call_count == 1
    auth = col.sync_collection.call_args.args[0]
    assert auth.hkey == "hkey-1"


def test_cached_key_endpoint_and_new_endpoint(fake: Fake, tmp_path: Path) -> None:
    s, col, _ = fake
    key = tmp_path / "prof"
    key.mkdir(parents=True)
    (key / "sync.key").write_text(json.dumps({"hkey": "cached", "endpoint": "https://s1/"}))
    col.sync_collection.return_value = _resp(
        SyncCollectionResponse.NO_CHANGES, new_endpoint="https://s2/"
    )
    s.sync()
    col.sync_login.assert_not_called()
    auth = col.sync_collection.call_args.args[0]
    assert auth.hkey == "cached"
    assert auth.endpoint == "https://s2/"  # updated in place from new_endpoint


def test_garbage_key_file_falls_back_to_login(fake: Fake, tmp_path: Path) -> None:
    s, col, _ = fake
    (tmp_path / "prof").mkdir(parents=True)
    (tmp_path / "prof" / "sync.key").write_text("not json")
    s.sync()
    col.sync_login.assert_called_once()
    assert json.loads((tmp_path / "prof" / "sync.key").read_text())["hkey"] == "hkey-1"


def test_auth_error_with_cached_key_retries_login_once(fake: Fake, tmp_path: Path) -> None:
    s, col, _ = fake
    (tmp_path / "prof").mkdir(parents=True)
    key = tmp_path / "prof" / "sync.key"
    key.write_text(json.dumps({"hkey": "stale", "endpoint": None}))
    col.sync_collection.side_effect = [
        _sync_error(SyncErrorKind.AUTH),
        _resp(SyncCollectionResponse.NO_CHANGES),
    ]
    result = s.sync()
    assert result.outcome is SyncOutcome.NO_CHANGES
    col.sync_login.assert_called_once_with(
        username="u@example.com", password="pw-secret", endpoint=None
    )
    assert json.loads(key.read_text())["hkey"] == "hkey-1"
    assert col.sync_collection.call_count == 2


def test_auth_error_twice_maps_to_sync_auth_failed(fake: Fake, tmp_path: Path) -> None:
    s, col, _ = fake
    (tmp_path / "prof").mkdir(parents=True)
    (tmp_path / "prof" / "sync.key").write_text(json.dumps({"hkey": "stale"}))
    col.sync_collection.side_effect = _sync_error(SyncErrorKind.AUTH)
    with pytest.raises(ApiError) as ei:
        s.sync()
    assert ei.value.code == "sync_auth_failed" and ei.value.status == 502
    assert ei.value.retryable is False
    assert s.last_sync_error == "SyncError"
    assert col.sync_login.call_count == 1


def test_auth_error_right_after_fresh_login_retries_once_then_fails(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_collection.side_effect = _sync_error(SyncErrorKind.AUTH)
    with pytest.raises(ApiError) as ei:
        s.sync()
    assert ei.value.code == "sync_auth_failed"
    # The key was obtained by this very call, so there is nothing stale to retry with.
    assert col.sync_login.call_count == 1
    assert col.sync_collection.call_count == 1
    assert s.last_sync_error == "SyncError"


def test_login_failure_maps_to_sync_auth_failed(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_login.side_effect = _sync_error(SyncErrorKind.AUTH)
    with pytest.raises(ApiError) as ei:
        s.sync()
    assert ei.value.code == "sync_auth_failed"
    col.sync_collection.assert_not_called()


def test_other_sync_error_is_upstream_error(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_collection.side_effect = _sync_error(SyncErrorKind.OTHER)
    with pytest.raises(UpstreamError) as ei:
        s.sync()
    assert ei.value.code == "sync_failed" and ei.value.retryable is True
    assert s.last_sync_error == "SyncError" and s.last_sync_at is None


def test_other_backend_error_maps_to_sync_failed(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_collection.side_effect = BackendError("database locked", None, None, None)
    with pytest.raises(ApiError) as ei:
        s.sync()
    assert ei.value.code == "sync_failed" and ei.value.status == 502
    assert ei.value.retryable is True and not isinstance(ei.value, UpstreamError)
    assert s.last_sync_error == "BackendError"


def test_full_sync_required_with_local_data_raises_and_flags(fake: Fake) -> None:
    s, col, db = fake
    db.notes = 5
    col.sync_collection.return_value = _resp(
        SyncCollectionResponse.FULL_SYNC, server_message="please full sync"
    )
    with pytest.raises(SyncRequiredFull) as ei:
        s.sync()
    assert ei.value.status == 409 and ei.value.code == "sync_required_full"
    assert ei.value.details == {"required": "full", "server_message": "please full sync"}
    assert s.full_sync_pending is True
    col.full_upload_or_download.assert_not_called()
    col.close_for_full_sync.assert_not_called()


def test_full_upload_required_raises_with_which(fake: Fake) -> None:
    s, col, db = fake
    db.cards = 1
    col.sync_collection.return_value = _resp(SyncCollectionResponse.FULL_UPLOAD)
    with pytest.raises(SyncRequiredFull) as ei:
        s.sync()
    assert ei.value.details["required"] == "full_upload"


def test_full_sync_on_empty_collection_downloads_without_backup(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_collection.return_value = _resp(SyncCollectionResponse.FULL_SYNC, server_media_usn=7)
    result = s.sync()
    assert result.outcome is SyncOutcome.DOWNLOADED and result.local_replaced is True
    col.close_for_full_sync.assert_called_once()
    col.full_upload_or_download.assert_called_once_with(auth=ANY, server_usn=7, upload=False)
    col.reopen.assert_called_once_with(after_full_sync=True)
    col.sync_media.assert_called_once()
    assert not s.backups_dir().exists()
    assert s.full_sync_pending is False


def test_full_download_required_backs_up_when_not_empty(fake: Fake) -> None:
    s, col, db = fake
    db.notes = 3
    col.sync_collection.return_value = _resp(SyncCollectionResponse.FULL_DOWNLOAD)
    result = s.sync()
    assert result.outcome is SyncOutcome.DOWNLOADED and result.local_replaced is True
    backups = list(s.backups_dir().glob("collection-*-pre-bootstrap.anki2"))
    assert len(backups) == 1
    col.full_upload_or_download.assert_called_once_with(auth=ANY, server_usn=0, upload=False)


def test_force_full_upload_takes_backup_and_uploads(fake: Fake) -> None:
    s, col, db = fake
    db.notes = 3
    result = s.sync(force_full="upload")  # server said NO_CHANGES; admin insists
    assert result.outcome is SyncOutcome.UPLOADED and result.local_replaced is False
    assert result.to_dict()["outcome"] == "uploaded"
    col.full_upload_or_download.assert_called_once_with(auth=ANY, server_usn=0, upload=True)
    backups = list(s.backups_dir().glob("collection-*-pre-force-upload.anki2"))
    assert len(backups) == 1
    col.sync_media.assert_called_once()


def test_force_full_download_when_server_demands_full_sync(fake: Fake) -> None:
    s, col, db = fake
    db.notes = 3
    col.sync_collection.return_value = _resp(SyncCollectionResponse.FULL_SYNC)
    result = s.sync(force_full="download")
    assert result.outcome is SyncOutcome.DOWNLOADED and result.local_replaced is True
    col.full_upload_or_download.assert_called_once_with(auth=ANY, server_usn=0, upload=False)
    assert len(list(s.backups_dir().glob("collection-*-pre-force-download.anki2"))) == 1
    assert s.full_sync_pending is False


def test_full_sync_failure_reopens_and_records_error(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_collection.return_value = _resp(SyncCollectionResponse.FULL_DOWNLOAD)
    col.full_upload_or_download.side_effect = _sync_error(SyncErrorKind.OTHER)
    with pytest.raises(UpstreamError) as ei:
        s.sync()
    assert ei.value.code == "sync_failed"
    col.reopen.assert_called_once_with(after_full_sync=False)
    assert s.last_sync_error == "SyncError"
    col.sync_media.assert_not_called()


def test_media_states(fake: Fake) -> None:
    s, col, _ = fake
    assert s.sync().media == "synced"
    col.media_sync_status.side_effect = RuntimeError("media broke")
    assert s.sync().media == "failed: RuntimeError"
    col.media_sync_status.side_effect = None
    col.media_sync_status.return_value.active = True
    assert s.sync().media == "in_progress"  # _MEDIA_WAIT_SECONDS shortened by the fixture
    s.cfg.media_sync = False
    assert s.sync().media == "skipped"
    assert col.sync_collection.call_args.kwargs == {"sync_media": False}


def test_sync_status_without_credentials(tmp_path: Path) -> None:
    s = make_session(tmp_path)
    status = s.sync_status()
    assert status == {
        "last_sync_at": None,
        "last_result": None,
        "last_error": None,
        "full_sync_pending": False,
        "configured": False,
    }
    s.close()


def test_sync_status_with_remote(fake: Fake) -> None:
    s, col, _ = fake
    col.sync_status.return_value = SyncStatusResponse(required=SyncStatusResponse.FULL_SYNC)
    status = s.sync_status()
    assert status["configured"] is True
    assert status["remote"] == {"required": "full_sync", "has_changes": True}
    # The returned dict is assembled before the remote check, so read the attribute.
    assert s.full_sync_pending is True
    col.sync_status.return_value = SyncStatusResponse(required=SyncStatusResponse.NO_CHANGES)
    status = s.sync_status()
    assert status["remote"] == {"required": "no_changes", "has_changes": False}
    assert s.full_sync_pending is False
    col.sync_login.side_effect = _sync_error(SyncErrorKind.AUTH)
    (s.profile_dir / "sync.key").unlink()
    assert s.sync_status()["remote"] == {"error": "sync_auth_failed"}


def test_sync_without_credentials_raises(tmp_path: Path) -> None:
    s = make_session(tmp_path)
    with pytest.raises(ApiError) as ei:
        s.sync()
    assert ei.value.code == "sync_not_configured"
    s.close()
