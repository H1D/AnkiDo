# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ankido`` command line: serve, manage tokens, sync, back up."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from ankido import __version__
from ankido.config import Config, load_config
from ankido.errors import ApiError
from ankido.logging import configure_logging
from ankido.store import SCOPES, Store, parse_scopes

_DURATION_RE = re.compile(r"^(\d+)([smhdy])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "y": 365 * 86400}


def _parse_duration(text: str) -> int:
    m = _DURATION_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError("duration must look like 30d, 12h, 1y")
    return int(m.group(1)) * _UNITS[m.group(2)]


def _fmt_ts(ts: int | None) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ankido", description="Headless API for AnkiWeb collections")
    p.add_argument("--config", "-c", help="path to ankido.yaml (default: $ANKIDO_CONFIG)")
    p.add_argument("--version", action="version", version=f"ankido {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the HTTP service")
    s.add_argument("--bind", help="override server.bind")
    s.add_argument("--port", type=int, help="override server.port")

    t = sub.add_parser("token", help="manage API tokens")
    tsub = t.add_subparsers(dest="token_command", required=True)
    tc = tsub.add_parser("create", help="create a token (secret shown once)")
    tc.add_argument("--profile", help="profile the token is bound to (omit with --admin)")
    tc.add_argument("--scopes", default="read,review", help=f"comma list of {','.join(SCOPES)}")
    tc.add_argument("--name", default="", help="label, e.g. 'kitchen-reader'")
    tc.add_argument("--expires", type=_parse_duration, help="e.g. 90d, 1y (default: never)")
    tc.add_argument("--admin", action="store_true", help="global admin token (all profiles)")
    tl = tsub.add_parser("list", help="list tokens")
    tl.add_argument("--profile")
    tr = tsub.add_parser("revoke", help="revoke a token by id")
    tr.add_argument("token_id")

    pr = sub.add_parser("profile", help="inspect profiles")
    psub = pr.add_subparsers(dest="profile_command", required=True)
    psub.add_parser("list", help="list configured profiles")

    sy = sub.add_parser("sync", help="sync one profile now (service must not be running)")
    sy.add_argument("profile")
    sy.add_argument("--force-full", choices=["upload", "download"], help="ADMIN: full sync")
    sy.add_argument("--yes", action="store_true", help="confirm a --force-full")

    b = sub.add_parser(
        "backup", help="back up a profile's collection (service must not be running)"
    )
    b.add_argument("profile")

    sub.add_parser("check-config", help="validate the config and credentials, then exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        return _dispatch(args, config)
    except KeyboardInterrupt:
        return 130
    except (ApiError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.command == "serve":
        return _serve(args, config)
    if args.command == "token":
        return _token(args, config)
    if args.command == "profile":
        for name, pcfg in config.profiles.items():
            print(f"{name}\t{pcfg.collection}\tautosync={pcfg.autosync}")
        return 0
    if args.command == "check-config":
        return _check_config(config)
    if args.command in ("sync", "backup"):
        return _offline_op(args, config)
    return 2


def _serve(args: argparse.Namespace, config: Config) -> int:
    import uvicorn

    from ankido.api.app import create_app

    configure_logging(config.server.log_level)
    app = create_app(config)
    uvicorn.run(
        app,
        host=args.bind or config.server.bind,
        port=args.port or config.server.port,
        log_config=None,
        access_log=False,
        server_header=False,
        # Forwarded headers are interpreted by Ankido itself (server.trusted_proxies), against
        # the real peer address; uvicorn rewriting the peer first would hide it.
        proxy_headers=False,
    )
    return 0


def _token(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.state_db_path)
    try:
        if args.token_command == "create":
            if args.admin:
                profile = None
                scopes = frozenset({"admin"})
            else:
                if not args.profile:
                    print("error: --profile is required (or --admin)", file=sys.stderr)
                    return 2
                if args.profile not in config.profiles:
                    print(f"error: unknown profile {args.profile!r}", file=sys.stderr)
                    return 2
                profile = args.profile
                scopes = parse_scopes(args.scopes)
            raw, rec = store.create_token(
                profile=profile, scopes=scopes, name=args.name, expires_in_seconds=args.expires
            )
            print(f"token id:  {rec.id}")
            print(f"profile:   {rec.profile or '(all, admin)'}")
            print(f"scopes:    {','.join(sorted(rec.scopes))}")
            print(f"expires:   {_fmt_ts(rec.expires_at)}")
            print()
            print("This is the only time the secret is shown:")
            print()
            print(f"  {raw}")
            return 0
        if args.token_command == "list":
            rows = store.list_tokens(args.profile)
            print(
                f"{'id':10} {'profile':14} {'scopes':26} {'name':18} "
                f"{'expires':17} {'last used':17} state"
            )
            for r in rows:
                state = "revoked" if r.revoked_at else ("expired" if not r.is_valid() else "active")
                print(
                    f"{r.id:10} {r.profile or '(admin)':14} {','.join(sorted(r.scopes)):26} "
                    f"{r.name[:18]:18} {_fmt_ts(r.expires_at):17} "
                    f"{_fmt_ts(r.last_used_at):17} {state}"
                )
            return 0
        if args.token_command == "revoke":
            ok = store.revoke_token(args.token_id)
            print("revoked" if ok else "no active token with that id")
            return 0 if ok else 1
    finally:
        store.close()
    return 2


def _check_config(config: Config) -> int:
    from ankido.config import load_credentials

    ok = True
    for name, pcfg in config.profiles.items():
        try:
            creds = load_credentials(name, pcfg)
            status = f"credentials for {creds.username}" if creds else "no credentials (sync off)"
        except Exception as exc:
            status = f"ERROR: {exc}"
            ok = False
        exists = "exists" if pcfg.collection.is_file() else "will be created"
        print(f"{name}: {status}; collection {exists}")
    print("config OK" if ok else "config has errors")
    return 0 if ok else 1


def _offline_op(args: argparse.Namespace, config: Config) -> int:
    from ankido.collection.session import Session
    from ankido.config import load_credentials

    configure_logging(config.server.log_level)
    name = args.profile
    if name not in config.profiles:
        print(f"error: unknown profile {name!r}", file=sys.stderr)
        return 2
    pcfg = config.profiles[name]
    session = Session(name, pcfg, config.profile_dir(name), load_credentials(name, pcfg))
    store = Store(config.state_db_path)
    try:
        if args.command == "backup":
            print(session.backup("manual"))
            store.audit(token_id="cli", profile=name, action="backup", outcome="ok")
            return 0
        force = args.force_full
        if force and not args.yes:
            print(
                "error: --force-full needs --yes; it overwrites one side. A backup is taken first.",
                file=sys.stderr,
            )
            return 2
        if force:
            store.audit(
                token_id="cli", profile=name, action=f"force_full_{force}", outcome="requested"
            )
        result = session.sync(force_full=force)
        store.audit(
            token_id="cli",
            profile=name,
            action="sync" if not force else f"force_full_{force}",
            outcome=result.outcome.value,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    finally:
        session.close()
        store.close()


if __name__ == "__main__":
    sys.exit(main())
