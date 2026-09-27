# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owns the shared services (store, auth, limiter, media resolver) and one worker per profile."""

from __future__ import annotations

from typing import Any

from ankido.auth import Authenticator
from ankido.collection.media import MediaResolver
from ankido.collection.session import SyncResult
from ankido.config import Config, load_credentials
from ankido.errors import NotFound
from ankido.logging import get_logger
from ankido.metrics import Metrics
from ankido.ratelimit import RateLimiter
from ankido.store import Store
from ankido.worker import ProfileWorker

log = get_logger("ankido.supervisor")


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config.state_db_path)
        self.auth = Authenticator(self.store)
        self.limiter = RateLimiter(config.server.rate_limits)
        self.media = MediaResolver(config.media)
        self.metrics = Metrics()
        self.workers: dict[str, ProfileWorker] = {}
        for name, pcfg in config.profiles.items():
            creds = load_credentials(name, pcfg)
            if creds is None:
                log.warning(
                    "profile has no AnkiWeb credentials; sync disabled", fields={"profile": name}
                )
            self.workers[name] = ProfileWorker(
                name,
                pcfg,
                config.server,
                config.profile_dir(name),
                creds,
                on_sync=self._record_sync,
            )

    def start(self) -> None:
        self.store.journal_prune()
        for w in self.workers.values():
            w.start()

    def stop(self) -> None:
        for w in self.workers.values():
            w.stop()
        self.store.close()

    def worker(self, profile: str) -> ProfileWorker:
        try:
            return self.workers[profile]
        except KeyError:
            raise NotFound(f"profile {profile!r} not found", code="profile_not_found") from None

    def _record_sync(self, profile: str, result: SyncResult | Exception) -> None:
        if isinstance(result, Exception):
            self.metrics.inc("sync_total", profile=profile, outcome="error")
            log.warning("sync failed", fields={"profile": profile, "error": type(result).__name__})
        else:
            self.metrics.inc("sync_total", profile=profile, outcome=result.outcome.value)
            log.info("sync done", fields={"profile": profile, **result.to_dict()})

    def profiles_status(self) -> list[dict[str, Any]]:
        from ankido.collection.ops import profile_status
        from ankido.oauth import oauth_status

        oauth = oauth_status(self.config)
        out: list[dict[str, Any]] = []
        for w in self.workers.values():
            status = profile_status(w.session)
            status["queue_depth"] = w.queue_depth
            status["syncing"] = w.syncing
            status["current_op"] = w.current_op
            status["oauth"] = oauth
            out.append(status)
        return out
