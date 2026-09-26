# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ankido.config import (
    Config,
    Credentials,
    ProfileConfig,
    find_config_path,
    load_config,
    load_credentials,
)


def _raw(tmp_path: Path, profiles: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    if profiles is None:
        profiles = {"alice": {"collection": str(tmp_path / "a.anki2")}}
    return {"data_dir": str(tmp_path), "profiles": profiles, **extra}


def test_valid_minimal_config_and_defaults(tmp_path: Path) -> None:
    cfg = Config.model_validate(_raw(tmp_path))
    assert cfg.server.bind == "127.0.0.1" and cfg.server.port == 8765
    assert cfg.server.rate_limits.write.burst == 40
    assert cfg.media.url_allowlist == [] and cfg.media.max_bytes == 8 * 1024 * 1024
    p = cfg.profiles["alice"]
    assert p.autosync == "after_write" and p.nightly_at == "03:30"
    assert p.allow_schema_upgrade is False and p.backups.keep == 10
    assert cfg.state_db_path == tmp_path / "ankido.db"
    assert cfg.profile_dir("alice") == tmp_path / "alice"


@pytest.mark.parametrize("name", ["Alice", "a b", "-x", "", "x" * 33, "ünï", "a.b"])
def test_bad_profile_name_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"profile name|at least one profile"):
        Config.model_validate(_raw(tmp_path, {name: {"collection": "/x.anki2"}}))


def test_no_profiles_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one profile"):
        Config.model_validate(_raw(tmp_path, {}))


@pytest.mark.parametrize("bad", ["25:00", "3:30", "03:60", "0330", "night"])
def test_bad_nightly_at_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="nightly_at must be HH:MM"):
        Config.model_validate(
            _raw(tmp_path, {"alice": {"collection": "/x.anki2", "nightly_at": bad}})
        )


def test_good_nightly_at(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        _raw(tmp_path, {"alice": {"collection": "/x.anki2", "nightly_at": "23:59"}})
    )
    assert cfg.profiles["alice"].nightly_at == "23:59"


def test_unknown_keys_rejected_at_every_level(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"extra|not permitted"):
        Config.model_validate(_raw(tmp_path, bogus=1))
    with pytest.raises(ValueError, match=r"extra|not permitted"):
        Config.model_validate(_raw(tmp_path, server={"bogus": 1}))
    with pytest.raises(ValueError, match=r"extra|not permitted"):
        Config.model_validate(_raw(tmp_path, {"alice": {"collection": "/x", "bogus": 1}}))


def test_yaml_bare_off_and_quoted_off_both_mean_off(tmp_path: Path) -> None:
    text = (
        f"data_dir: {tmp_path}\n"
        "profiles:\n"
        "  alice: {collection: /a.anki2, autosync: off}\n"
        '  bob: {collection: /b.anki2, autosync: "off"}\n'
    )
    path = tmp_path / "ankido.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.profiles["alice"].autosync == "off"
    assert cfg.profiles["bob"].autosync == "off"
    with pytest.raises(ValueError):
        Config.model_validate(_raw(tmp_path, {"alice": {"collection": "/x", "autosync": "on"}}))


def test_load_config_requires_mapping(tmp_path: Path) -> None:
    path = tmp_path / "ankido.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top level must be a mapping"):
        load_config(path)
    path.write_text("", encoding="utf-8")  # empty file -> {} -> missing profiles
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ankido.yaml"
    path.write_text(yaml.safe_dump(_raw(tmp_path)), encoding="utf-8")
    assert load_config(path).profiles["alice"].collection == tmp_path / "a.anki2"


# ---- credentials -------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANKIDO_ALLOW_INSECURE_SECRETS", raising=False)
    monkeypatch.delenv("ANKIDO_PROFILE_ALICE_USERNAME", raising=False)
    monkeypatch.delenv("ANKIDO_PROFILE_ALICE_PASSWORD", raising=False)


def _env_file(tmp_path: Path, text: str, mode: int) -> Path:
    f = tmp_path / "alice.env"
    f.write_text(text, encoding="utf-8")
    f.chmod(mode)
    return f


@pytest.mark.usefixtures("clean_env")
def test_credentials_file_mode_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "# comment line\n"
        "\n"
        'export ANKIWEB_USERNAME="alice@example.com"\n'
        "ANKIWEB_PASSWORD='s3cret=with=equals'\n"
        "garbage line without equals\n"
    )
    f = _env_file(tmp_path, text, 0o644)
    pc = ProfileConfig(collection=tmp_path / "c.anki2", credentials=f)
    with pytest.raises(PermissionError, match="chmod 600"):
        load_credentials("alice", pc)

    monkeypatch.setenv("ANKIDO_ALLOW_INSECURE_SECRETS", "1")
    creds = load_credentials("alice", pc)
    assert creds == Credentials(username="alice@example.com", password="s3cret=with=equals")

    monkeypatch.delenv("ANKIDO_ALLOW_INSECURE_SECRETS")
    f.chmod(0o600)
    assert load_credentials("alice", pc) == creds
    f.chmod(0o400)
    assert load_credentials("alice", pc) == creds


@pytest.mark.usefixtures("clean_env")
def test_credentials_file_plain_keys_accepted(tmp_path: Path) -> None:
    f = _env_file(tmp_path, "USERNAME=u\nPASSWORD=p\n", 0o600)
    pc = ProfileConfig(collection=tmp_path / "c.anki2", credentials=f)
    assert load_credentials("alice", pc) == Credentials(username="u", password="p")


@pytest.mark.usefixtures("clean_env")
def test_credentials_file_missing_or_incomplete(tmp_path: Path) -> None:
    pc = ProfileConfig(collection=tmp_path / "c.anki2", credentials=tmp_path / "nope.env")
    with pytest.raises(FileNotFoundError, match="credentials file"):
        load_credentials("alice", pc)
    f = _env_file(tmp_path, "ANKIWEB_USERNAME=only\n", 0o600)
    pc = ProfileConfig(collection=tmp_path / "c.anki2", credentials=f)
    with pytest.raises(ValueError, match="expected ANKIWEB_USERNAME and ANKIWEB_PASSWORD"):
        load_credentials("alice", pc)


@pytest.mark.usefixtures("clean_env")
def test_credentials_from_process_env_take_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _env_file(tmp_path, "ANKIWEB_USERNAME=file\nANKIWEB_PASSWORD=file\n", 0o644)
    pc = ProfileConfig(collection=tmp_path / "c.anki2", credentials=f)
    monkeypatch.setenv("ANKIDO_PROFILE_ALICE_USERNAME", "env-user")
    monkeypatch.setenv("ANKIDO_PROFILE_ALICE_PASSWORD", "env-pass")
    # the insecure file is never read when env wins
    assert load_credentials("alice", pc) == Credentials(username="env-user", password="env-pass")


def test_credentials_env_key_for_dashed_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANKIDO_PROFILE_MY_PROF_USERNAME", "u")
    monkeypatch.setenv("ANKIDO_PROFILE_MY_PROF_PASSWORD", "p")
    pc = ProfileConfig(collection=Path("/x.anki2"))
    assert load_credentials("my-prof", pc) == Credentials(username="u", password="p")


@pytest.mark.usefixtures("clean_env")
def test_no_credentials_returns_none() -> None:
    assert load_credentials("alice", ProfileConfig(collection=Path("/x.anki2"))) is None


def test_credentials_repr_hides_password() -> None:
    c = Credentials(username="u@example.com", password="hunter2-long")
    assert "hunter2" not in repr(c)
    assert "u@example.com" in repr(c)


# ---- find_config_path --------------------------------------------------------------------


def test_find_config_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANKIDO_CONFIG", raising=False)
    with pytest.raises(FileNotFoundError, match="no config found"):
        find_config_path()
    (tmp_path / "ankido.yaml").touch()
    assert find_config_path() == Path("ankido.yaml")
    monkeypatch.setenv("ANKIDO_CONFIG", str(tmp_path / "from-env.yaml"))
    assert find_config_path() == tmp_path / "from-env.yaml"
    assert find_config_path(tmp_path / "explicit.yaml") == tmp_path / "explicit.yaml"
    monkeypatch.setenv("ANKIDO_CONFIG", "")  # empty env var is ignored
    assert find_config_path() == Path("ankido.yaml")
