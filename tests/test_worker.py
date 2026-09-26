# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ankido.collection.session import Session, SyncOutcome, SyncResult
from ankido.config import AutosyncMode, Credentials, ProfileConfig, ServerConfig
from ankido.errors import InternalError, ProfileBusy
from ankido.worker import (
    PRIORITY_READ,
    PRIORITY_SYNC,
    PRIORITY_WRITE,
    ProfileWorker,
    _next_time_of_day,  # pyright: ignore[reportPrivateUsage]
)

CREDS = Credentials(username="u@example.com", password="pw-secret")


def make_worker(
    tmp_path: Path,
    *,
    autosync: AutosyncMode = "off",
    debounce: float = 0.2,
    idle: float = 0.0,
    timeout: float = 30.0,
    creds: Credentials | None = None,
    events: list[Any] | None = None,
) -> ProfileWorker:
    pcfg = ProfileConfig(
        collection=tmp_path / "w" / "collection.anki2",
        autosync=autosync,
        after_write_debounce_seconds=debounce,
    )
    server = ServerConfig(operation_timeout_seconds=timeout, idle_close_seconds=idle)

    def on_sync(profile: str, result: SyncResult | Exception) -> None:
        if events is not None:
            events.append((profile, result))

    return ProfileWorker("w", pcfg, server, tmp_path / "w", creds, on_sync=on_sync)


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[ProfileWorker]:
    w = make_worker(tmp_path)
    w.start()
    yield w
    w.stop()


def _ok_sync() -> SyncResult:
    return SyncResult(
        outcome=SyncOutcome.NO_CHANGES,
        local_replaced=False,
        server_message="",
        media="synced",
        duration_ms=1,
    )


@pytest.fixture
def fake_sync(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    calls: list[str | None] = []

    def sync(self: Session, *, force_full: str | None = None) -> SyncResult:
        calls.append(force_full)
        return _ok_sync()

    monkeypatch.setattr(Session, "sync", sync)
    return calls


def test_submit_runs_on_worker_thread_and_returns(worker: ProfileWorker) -> None:
    assert worker.submit("who", lambda s: threading.current_thread().name) == "ankido-w"
    assert worker.queue_depth == 0
    assert worker.current_op is None


def test_reads_run_before_writes_when_both_queued(worker: ProfileWorker) -> None:
    order: list[str] = []
    started = threading.Event()

    def slow(s: Session) -> None:
        started.set()
        time.sleep(0.3)
        order.append("slow")

    def write(s: Session) -> None:
        order.append("write")

    def read(s: Session) -> None:
        order.append("read")

    worker.submit_async("slow", slow, priority=PRIORITY_READ)
    assert started.wait(2)
    f_write = worker.submit_async("write", write, priority=PRIORITY_WRITE)
    f_sync = worker.submit_async("sync-ish", lambda s: order.append("sync"), priority=PRIORITY_SYNC)
    f_read = worker.submit_async("read", read, priority=PRIORITY_READ)
    assert worker.queue_depth == 3
    f_write.result(5)
    f_read.result(5)
    f_sync.result(5)
    assert order == ["slow", "read", "write", "sync"]


def test_timeout_raises_profile_busy(tmp_path: Path) -> None:
    w = make_worker(tmp_path, timeout=0.2)
    w.start()
    try:
        w.submit_async("slow", lambda s: time.sleep(0.6), priority=PRIORITY_READ)
        with pytest.raises(ProfileBusy, match="timed out") as ei:
            w.submit("read", lambda s: 1)
        assert ei.value.status == 503 and ei.value.retryable is True
        # explicit timeout wins over the server default
        w.submit_async("slow2", lambda s: time.sleep(0.4), priority=PRIORITY_READ)
        assert w.submit("read", lambda s: 2, timeout=5) == 2
    finally:
        w.stop()


def test_exception_inside_op_becomes_internal_error_and_worker_survives(
    worker: ProfileWorker,
) -> None:
    def boom(s: Session) -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(InternalError) as ei:
        worker.submit("boom", boom)
    assert ei.value.status == 500
    assert "boom failed: RuntimeError" in ei.value.message
    assert "kaboom" not in ei.value.message  # no raw exception text leaks to clients
    assert worker.submit("after", lambda s: "alive") == "alive"


def test_api_errors_pass_through_unchanged(worker: ProfileWorker) -> None:
    def denied(s: Session) -> None:
        raise ProfileBusy("custom")

    with pytest.raises(ProfileBusy, match="custom"):
        worker.submit("denied", denied)


def test_idle_close(tmp_path: Path) -> None:
    w = make_worker(tmp_path, idle=0.3)
    w.start()
    try:
        w.submit("open", lambda s: s.require())
        assert w.session.is_open
        deadline = time.monotonic() + 3
        while w.session.is_open and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not w.session.is_open
        # reopens lazily on the next op
        w.submit("reopen", lambda s: s.require())
        assert w.session.is_open
    finally:
        w.stop()


def test_after_write_debounce_triggers_sync(tmp_path: Path, fake_sync: list[str | None]) -> None:
    events: list[Any] = []
    w = make_worker(tmp_path, autosync="after_write", debounce=0.2, creds=CREDS, events=events)
    w.start()
    try:
        w.note_write()
        w.note_write()  # re-arms; still one sync
        deadline = time.monotonic() + 3
        while not fake_sync and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        assert fake_sync == [None]
        assert len(events) == 1
        assert events[0][0] == "w" and isinstance(events[0][1], SyncResult)
        assert w.syncing is False
    finally:
        w.stop()
    assert fake_sync == [None]  # stop() did not sync again


def test_note_write_is_noop_without_credentials_or_when_autosync_off(
    tmp_path: Path, fake_sync: list[str | None]
) -> None:
    for kw in ({"autosync": "after_write", "creds": None}, {"autosync": "off", "creds": CREDS}):
        w = make_worker(tmp_path, debounce=0.1, **kw)  # type: ignore[arg-type]
        w.start()
        try:
            w.note_write()
            time.sleep(0.3)
            assert fake_sync == []
        finally:
            w.stop()


def test_stop_flushes_pending_sync_and_closes(tmp_path: Path, fake_sync: list[str | None]) -> None:
    w = make_worker(tmp_path, autosync="after_write", debounce=100.0, creds=CREDS)
    w.start()
    w.submit("open", lambda s: s.require())
    w.note_write()
    w.stop()
    assert fake_sync == [None]
    assert not w.session.is_open
    with pytest.raises(ProfileBusy, match="shutting down"):
        w.submit("late", lambda s: 1)
    with pytest.raises(ProfileBusy):
        w.submit_async("late", lambda s: 1, priority=PRIORITY_READ)


def test_sync_method_reports_result_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    w = make_worker(tmp_path, creds=CREDS, events=events)
    w.start()
    try:
        calls: list[str | None] = []

        def sync(self: Session, *, force_full: str | None = None) -> SyncResult:
            calls.append(force_full)
            if force_full == "upload":
                raise RuntimeError("upstream exploded")
            return _ok_sync()

        monkeypatch.setattr(Session, "sync", sync)
        assert w.sync().outcome is SyncOutcome.NO_CHANGES
        with pytest.raises(InternalError):
            w.sync(force_full="upload")
        assert calls == [None, "upload"]
        assert isinstance(events[0][1], SyncResult)
        assert isinstance(events[1][1], RuntimeError)
        assert w.syncing is False
    finally:
        w.stop()


def test_nightly_schedule_is_armed_on_start(tmp_path: Path) -> None:
    w = make_worker(tmp_path, autosync="nightly", creds=CREDS)
    w.start()
    try:
        nxt = w._next_nightly_at  # pyright: ignore[reportPrivateUsage]
        assert nxt is not None and 0 < nxt - time.time() <= 86400
    finally:
        w.stop()


def test_next_time_of_day_is_in_the_future_within_a_day() -> None:
    for hhmm in ("00:00", "12:30", "23:59"):
        ts = _next_time_of_day(hhmm)
        assert 0 < ts - time.time() <= 86400 + 1
