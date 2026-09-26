# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures: a two-profile config in tmp_path, a store, tokens, a supervisor, an app.

``import ankido`` must run before any ``anki.*`` import (the circular-import guard lives in
``ankido/__init__.py``). pytest loads this conftest before any test module, so importing an
``ankido`` module here is what makes the direct ``anki.*`` imports in test modules safe.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ankido  # noqa: F401  # pyright: ignore[reportUnusedImport]
from ankido.api.app import create_app
from ankido.collection.media import MediaResolver
from ankido.collection.session import Session
from ankido.config import Config, load_config
from ankido.store import Store
from ankido.supervisor import Supervisor

FULL_SCOPES = frozenset({"read", "add", "review", "sync"})

ConfigMaker = Callable[..., Config]
Seeder = Callable[..., list[dict[str, Any]]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def base_config_dict(tmp_path: Path) -> dict[str, Any]:
    data_dir = tmp_path / "data"
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    return {
        "data_dir": str(data_dir),
        "server": {"idle_close_seconds": 2.0, "max_body_bytes": 64 * 1024},
        "media": {
            "url_allowlist": ["media.example.com", "*.example.com"],
            "path_allowlist": [str(allowed)],
            "max_bytes": 1024 * 1024,
        },
        "profiles": {
            "alice": {
                "collection": str(data_dir / "alice" / "collection.anki2"),
                # quoted on purpose: a bare YAML `off` is False (config accepts both)
                "autosync": "off",
            },
            "bob": {
                "collection": str(data_dir / "bob" / "collection.anki2"),
                "autosync": "off",
            },
        },
    }


@pytest.fixture
def make_config(tmp_path: Path) -> ConfigMaker:
    """Write ``ankido.yaml`` into tmp_path (with optional deep-merged overrides) and load it."""

    def make(**overrides: Any) -> Config:
        raw = _deep_merge(base_config_dict(tmp_path), overrides)
        path = tmp_path / "ankido.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return load_config(path)

    return make


@pytest.fixture
def config(make_config: ConfigMaker) -> Config:
    return make_config()


@pytest.fixture
def config_path(config: Config, tmp_path: Path) -> Path:
    return tmp_path / "ankido.yaml"


@pytest.fixture
def store(config: Config) -> Iterator[Store]:
    s = Store(config.state_db_path)
    yield s
    s.close()


@pytest.fixture
def alice_token(store: Store) -> str:
    raw, _ = store.create_token(profile="alice", scopes=FULL_SCOPES, name="alice-full")
    return raw


@pytest.fixture
def alice_read_token(store: Store) -> str:
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"read"}), name="alice-ro")
    return raw


@pytest.fixture
def alice_admin_token(store: Store) -> str:
    raw, _ = store.create_token(profile="alice", scopes=frozenset({"admin"}), name="alice-adm")
    return raw


@pytest.fixture
def bob_token(store: Store) -> str:
    raw, _ = store.create_token(profile="bob", scopes=FULL_SCOPES, name="bob-full")
    return raw


@pytest.fixture
def admin_token(store: Store) -> str:
    raw, _ = store.create_token(profile=None, scopes=frozenset({"admin"}), name="root")
    return raw


@pytest.fixture
def supervisor(config: Config) -> Iterator[Supervisor]:
    """A started supervisor. Do not combine with ``client`` (two workers per collection)."""
    sup = Supervisor(config)
    sup.start()
    yield sup
    sup.stop()


@pytest.fixture
def app(config: Config) -> FastAPI:
    return create_app(config)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session(config: Config) -> Iterator[Session]:
    """An open throwaway collection for profile ``alice`` (no credentials)."""
    s = Session("alice", config.profiles["alice"], config.profile_dir("alice"), None)
    s.open()
    yield s
    s.close()


@pytest.fixture
def resolver(config: Config) -> MediaResolver:
    return MediaResolver(config.media)


_seed_counter = itertools.count()


@pytest.fixture
def seed_notes(client: TestClient, alice_token: str) -> Seeder:
    """Add ``n`` synthetic Basic notes through the API; returns the per-note results."""

    def seed(
        n: int = 5,
        *,
        deck: str = "Test",
        model: str = "Basic",
        profile: str = "alice",
        token: str | None = None,
        prefix: str = "word",
    ) -> list[dict[str, Any]]:
        batch = next(_seed_counter)
        notes = [
            {
                "client_id": f"seed-{batch}-{i}",
                "fields": {"Front": f"{prefix} {batch}-{i}", "Back": f"meaning {i}"},
            }
            for i in range(n)
        ]
        r = client.post(
            f"/v1/p/{profile}/notes",
            json={"deck": deck, "model": model, "notes": notes},
            headers=auth(token or alice_token),
        )
        assert r.status_code == 200, r.text
        results: list[dict[str, Any]] = r.json()["results"]
        assert all(x["status"] == "added" for x in results), results
        return results

    return seed
