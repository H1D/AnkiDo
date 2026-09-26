# SPDX-License-Identifier: AGPL-3.0-or-later
"""One worker thread per profile: the only thing that ever touches that profile's collection.

Operations are submitted as callables and executed in priority order (reads before writes,
syncs last). The thread also handles idle-close and the ``after_write`` sync debounce.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from ankido.collection.session import Session, SyncResult
from ankido.config import Credentials, ProfileConfig, ServerConfig
from ankido.errors import ApiError, InternalError, ProfileBusy
from ankido.logging import get_logger

log = get_logger("ankido.worker")

T = TypeVar("T")

PRIORITY_READ = 0
PRIORITY_WRITE = 1
PRIORITY_SYNC = 2
_MAX_QUEUE_DEPTH = 500


class _Op:
    __slots__ = ("fn", "future", "name", "priority", "seq", "submitted")

    def __init__(self, priority: int, seq: int, name: str, fn: Callable[[Session], Any]) -> None:
        self.priority = priority
        self.seq = seq
        self.name = name
        self.fn = fn
        self.future: Future[Any] = Future()
        self.submitted = time.monotonic()

    def __lt__(self, other: _Op) -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


class ProfileWorker:
    def __init__(
        self,
        name: str,
        cfg: ProfileConfig,
        server: ServerConfig,
        profile_dir: Path,
        credentials: Credentials | None,
        *,
        on_sync: Callable[[str, SyncResult | Exception], None] | None = None,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self.server = server
        self.session = Session(name, cfg, profile_dir, credentials)
        self._heap: list[_Op] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=f"ankido-{name}", daemon=True)
        self._pending_sync_at: float | None = None  # after_write debounce deadline
        self._next_nightly_at: float | None = None
        self._on_sync = on_sync
        self.last_activity = time.monotonic()
        self.syncing = False
        self.current_op: str | None = None

    # ---- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self.cfg.autosync == "nightly":
            self._next_nightly_at = _next_time_of_day(self.cfg.nightly_at)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self._thread.join(timeout)

    @property
    def queue_depth(self) -> int:
        with self._cv:
            return len(self._heap)

    # ---- submission ---------------------------------------------------------------------

    def submit(
        self,
        name: str,
        fn: Callable[[Session], T],
        *,
        priority: int = PRIORITY_READ,
        timeout: float | None = None,
    ) -> T:
        """Run ``fn(session)`` on the worker thread and wait for its result."""
        with self._cv:
            if self._stop:
                raise ProfileBusy("service is shutting down")
            if len(self._heap) >= _MAX_QUEUE_DEPTH:
                raise ProfileBusy("profile operation queue is full")
            op = _Op(priority, next(self._seq), name, fn)
            heapq.heappush(self._heap, op)
            self._cv.notify()
        try:
            return op.future.result(timeout=timeout or self.server.operation_timeout_seconds)
        except TimeoutError as exc:
            raise ProfileBusy(f"operation {name!r} timed out waiting for the profile") from exc

    def submit_async(self, name: str, fn: Callable[[Session], T], *, priority: int) -> Future[T]:
        with self._cv:
            if self._stop:
                raise ProfileBusy("service is shutting down")
            op = _Op(priority, next(self._seq), name, fn)
            heapq.heappush(self._heap, op)
            self._cv.notify()
        return op.future

    def note_write(self) -> None:
        """Called after a successful write; arms the ``after_write`` debounce."""
        if self.cfg.autosync != "after_write" or self.session.credentials is None:
            return
        with self._cv:
            self._pending_sync_at = time.monotonic() + self.cfg.after_write_debounce_seconds
            self._cv.notify()

    def sync(self, *, force_full: str | None = None, timeout: float | None = None) -> SyncResult:
        return self.submit(
            "sync",
            lambda s: self._do_sync(s, force_full),
            priority=PRIORITY_SYNC,
            timeout=timeout or 600.0,
        )

    def sync_async(self) -> Future[SyncResult]:
        return self.submit_async("sync", lambda s: self._do_sync(s, None), priority=PRIORITY_SYNC)

    # ---- worker loop --------------------------------------------------------------------

    def _run(self) -> None:
        log.info("worker started", fields={"profile": self.name})
        while True:
            with self._cv:
                while not self._heap and not self._stop:
                    wait = self._time_until_timer()
                    self._cv.wait(timeout=wait)
                    if not self._heap:
                        self._fire_timers_locked()
                if self._stop and not self._heap:
                    break
                op = heapq.heappop(self._heap)
            self._execute(op)
        self._shutdown()

    def _execute(self, op: _Op) -> None:
        if op.future.cancelled():
            return
        self.current_op = op.name
        started = time.monotonic()
        try:
            result = op.fn(self.session)
        except ApiError as exc:
            op.future.set_exception(exc)
        except Exception as exc:
            log.exception("operation failed", fields={"profile": self.name, "op": op.name})
            op.future.set_exception(InternalError(f"{op.name} failed: {type(exc).__name__}"))
        else:
            op.future.set_result(result)
        finally:
            self.current_op = None
            self.last_activity = time.monotonic()
            log.debug(
                "op done",
                fields={
                    "profile": self.name,
                    "op": op.name,
                    "ms": int((time.monotonic() - started) * 1000),
                    "waited_ms": int((started - op.submitted) * 1000),
                },
            )

    def _time_until_timer(self) -> float | None:
        now = time.monotonic()
        candidates: list[float] = []
        if self._pending_sync_at is not None:
            candidates.append(self._pending_sync_at - now)
        if self._next_nightly_at is not None:
            candidates.append(self._next_nightly_at - time.time())
        if self.session.is_open and self.server.idle_close_seconds > 0:
            candidates.append(self.server.idle_close_seconds - (now - self.last_activity))
        if not candidates:
            return None
        return max(0.05, min(candidates))

    def _fire_timers_locked(self) -> None:
        """Called with the lock held while the queue is empty; enqueues timer-driven ops."""
        now = time.monotonic()
        if self._pending_sync_at is not None and now >= self._pending_sync_at:
            self._pending_sync_at = None
            self._enqueue_locked("autosync", lambda s: self._do_sync(s, None), PRIORITY_SYNC)
            return
        if self._next_nightly_at is not None and time.time() >= self._next_nightly_at:
            self._next_nightly_at = _next_time_of_day(self.cfg.nightly_at)
            self._enqueue_locked("nightly-sync", lambda s: self._do_sync(s, None), PRIORITY_SYNC)
            return
        if (
            self.session.is_open
            and self.server.idle_close_seconds > 0
            and now - self.last_activity >= self.server.idle_close_seconds
            and self._pending_sync_at is None
        ):
            self._enqueue_locked("idle-close", lambda s: s.close(), PRIORITY_SYNC)

    def _enqueue_locked(self, name: str, fn: Callable[[Session], Any], priority: int) -> None:
        heapq.heappush(self._heap, _Op(priority, next(self._seq), name, fn))

    def _do_sync(self, session: Session, force_full: str | None) -> SyncResult:
        self.syncing = True
        try:
            result = session.sync(force_full=force_full)
        except Exception as exc:
            if self._on_sync:
                self._on_sync(self.name, exc)
            raise
        else:
            if self._on_sync:
                self._on_sync(self.name, result)
            return result
        finally:
            self.syncing = False
            with self._cv:
                self._pending_sync_at = None

    def _shutdown(self) -> None:
        # Flush a pending after_write sync so nothing is lost, then close the collection.
        try:
            if self._pending_sync_at is not None and self.session.is_open:
                log.info("flushing pending sync on shutdown", fields={"profile": self.name})
                self._do_sync(self.session, None)
        except Exception:
            log.exception("shutdown sync failed", fields={"profile": self.name})
        finally:
            try:
                self.session.close()
            except Exception:
                log.exception("close failed", fields={"profile": self.name})
        with self._cv:
            for op in self._heap:
                op.future.set_exception(ProfileBusy("service is shutting down"))
            self._heap.clear()
        log.info("worker stopped", fields={"profile": self.name})


def _next_time_of_day(hhmm: str) -> float:
    hour, minute = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()
