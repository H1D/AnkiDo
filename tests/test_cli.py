# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from ankido import __version__
from ankido.cli import main
from ankido.errors import ApiError
from conftest import ConfigMaker


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """The CLI calls configure_logging(), which rebinds the root logger to captured stdout."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_token_create_list_revoke_flow(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    c = str(config_path)
    rc, out, _ = run(
        capsys, "--config", c, "token", "create", "--profile", "alice",
        "--scopes", "read,review", "--name", "kitchen-reader",
    )  # fmt: skip
    assert rc == 0, out
    m = re.search(r"token id:\s+(\w+)", out)
    assert m is not None
    tid = m.group(1)
    assert re.search(r"^\s+akd_\w+_[\w-]+$", out, re.M)
    assert "only time the secret is shown" in out
    assert "scopes:    read,review" in out and "expires:   -" in out

    rc, out, _ = run(capsys, "--config", c, "token", "list")
    assert rc == 0
    assert tid in out and "kitchen-reader" in out and "active" in out and "read,review" in out
    rc, out, _ = run(capsys, "--config", c, "token", "list", "--profile", "bob")
    assert rc == 0 and tid not in out

    rc, out, _ = run(capsys, "--config", c, "token", "revoke", tid)
    assert rc == 0 and out.strip() == "revoked"
    rc, out, _ = run(capsys, "--config", c, "token", "list")
    assert "revoked" in out
    rc, out, _ = run(capsys, "--config", c, "token", "revoke", tid)
    assert rc == 1 and "no active token" in out


def test_token_create_admin_and_expiry(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    c = str(config_path)
    rc, out, _ = run(capsys, "--config", c, "token", "create", "--admin")
    assert rc == 0 and "(all, admin)" in out and "scopes:    admin" in out
    rc, out, _ = run(
        capsys, "--config", c, "token", "create", "--profile", "bob", "--expires", "30d"
    )
    assert rc == 0
    assert re.search(r"expires:\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out)
    rc, out, _ = run(capsys, "--config", c, "token", "list")
    assert "expired" not in out


def test_token_create_errors(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = str(config_path)
    rc, _, err = run(capsys, "--config", c, "token", "create")
    assert rc == 2 and "--profile is required" in err
    rc, _, err = run(capsys, "--config", c, "token", "create", "--profile", "zed")
    assert rc == 2 and "unknown profile 'zed'" in err
    with pytest.raises(SystemExit) as ei:
        main(["--config", c, "token", "create", "--profile", "alice", "--expires", "soon"])
    assert ei.value.code == 2
    rc, _, err = run(
        capsys, "--config", c, "token", "create", "--profile", "alice", "--scopes", "delete"
    )
    assert rc == 1 and "unknown scope" in err


def test_check_config_ok_and_credential_errors(
    config_path: Path,
    make_config: ConfigMaker,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANKIDO_ALLOW_INSECURE_SECRETS", raising=False)
    rc, out, _ = run(capsys, "--config", str(config_path), "check-config")
    assert rc == 0 and "config OK" in out
    assert "alice: no credentials (sync off); collection will be created" in out

    env = tmp_path / "alice.env"
    env.write_text("ANKIWEB_USERNAME=alice@example.com\nANKIWEB_PASSWORD=pw\n", encoding="utf-8")
    env.chmod(0o644)
    make_config(profiles={"alice": {"credentials": str(env)}})
    rc, out, _ = run(capsys, "--config", str(config_path), "check-config")
    assert rc == 1 and "config has errors" in out and "alice: ERROR:" in out

    monkeypatch.setenv("ANKIDO_ALLOW_INSECURE_SECRETS", "1")
    rc, out, _ = run(capsys, "--config", str(config_path), "check-config")
    assert rc == 0 and "credentials for alice@example.com" in out
    assert "pw" not in out.replace("alice@example.com", "")


def test_profile_list(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _ = run(capsys, "--config", str(config_path), "profile", "list")
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("alice\t") and lines[0].endswith("\tautosync=off")
    assert lines[1].startswith("bob\t")


def test_sync_without_credentials_fails(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        rc = main(["--config", str(config_path), "sync", "alice"])
    except ApiError as exc:
        assert exc.code == "sync_not_configured"
    else:
        assert rc != 0


def test_sync_argument_errors(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = str(config_path)
    rc, _, err = run(capsys, "--config", c, "sync", "alice", "--force-full", "upload")
    assert rc == 2 and "--force-full needs --yes" in err
    rc, _, err = run(capsys, "--config", c, "sync", "zed")
    assert rc == 2 and "unknown profile 'zed'" in err
    with pytest.raises(SystemExit):
        main(["--config", c, "sync", "alice", "--force-full", "sideways"])


def test_backup_command(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _ = run(capsys, "--config", str(config_path), "backup", "alice")
    assert rc == 0
    paths = [ln for ln in out.splitlines() if ln.endswith(".anki2") and not ln.startswith("{")]
    assert len(paths) == 1
    p = Path(paths[0])
    assert p.is_file() and p.parent.name == "backups" and "-manual" in p.name


def test_missing_or_broken_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(capsys, "--config", str(tmp_path / "nope.yaml"), "profile", "list")
    assert rc == 2 and err.startswith("error:")
    broken = tmp_path / "broken.yaml"
    broken.write_text("profiles: {Bad Name: {collection: /x}}\n", encoding="utf-8")
    rc, _, err = run(capsys, "--config", str(broken), "profile", "list")
    assert rc == 2 and "profile name" in err


def test_config_from_env_and_version(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANKIDO_CONFIG", str(config_path))
    rc, out, _ = run(capsys, "profile", "list")
    assert rc == 0 and "alice" in out
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    assert f"ankido {__version__}" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main([])  # a command is required
