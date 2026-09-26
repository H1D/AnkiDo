# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve media attachments (``data`` / ``path`` / ``url``) into bytes, safely.

* ``url`` hosts must be on the configured allowlist, otherwise the service would be an SSRF proxy.
* ``path`` must be under one of the configured ``media.path_allowlist`` directories.
* Everything is capped at ``media.max_bytes``.
"""

from __future__ import annotations

import base64
import ipaddress
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ankido.config import MediaConfig
from ankido.errors import ApiError, UpstreamError

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_MAX_NAME = 120


def safe_filename(name: str, fallback_ext: str = "") -> str:
    name = Path(name).name.strip()  # drop any directory components
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    if not name:
        name = "media"
    if len(name) > _MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: _MAX_NAME - len(ext) - 1] + dot + ext) if dot else name[:_MAX_NAME]
    if "." not in name and fallback_ext:
        name += fallback_ext
    return name


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        e = entry.lower().rstrip(".")
        if e.startswith("*."):
            if host.endswith(e[1:]) or host == e[2:]:
                return True
        elif host == e:
            return True
    return False


class MediaResolver:
    def __init__(self, cfg: MediaConfig) -> None:
        self._cfg = cfg

    def resolve(
        self,
        *,
        filename: str | None,
        data: str | None,
        path: str | None,
        url: str | None,
    ) -> tuple[str, bytes]:
        sources = [s for s in (data, path, url) if s is not None]
        if len(sources) != 1:
            raise ApiError("exactly one of data, path, url is required", code="invalid_media")
        if data is not None:
            return self._from_data(filename, data)
        if path is not None:
            return self._from_path(filename, path)
        assert url is not None
        return self._from_url(filename, url)

    def _from_data(self, filename: str | None, data: str) -> tuple[str, bytes]:
        if not filename:
            raise ApiError("filename is required with inline data", code="invalid_media")
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise ApiError("data is not valid base64", code="invalid_media") from exc
        self._check_size(len(raw))
        return safe_filename(filename), raw

    def _from_path(self, filename: str | None, path: str) -> tuple[str, bytes]:
        p = Path(path)
        if not p.is_absolute():
            raise ApiError("path must be absolute", code="invalid_media")
        try:
            resolved = p.resolve(strict=True)
        except OSError as exc:
            raise ApiError(f"path not found: {path}", code="invalid_media") from exc
        if not any(_is_relative_to(resolved, root.resolve()) for root in self._cfg.path_allowlist):
            raise ApiError(
                "path is outside media.path_allowlist", code="media_path_not_allowed", status=403
            )
        size = resolved.stat().st_size
        self._check_size(size)
        return safe_filename(filename or resolved.name), resolved.read_bytes()

    def _from_url(self, filename: str | None, url: str) -> tuple[str, bytes]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ApiError("url must be http(s)", code="invalid_media")
        host = parts.hostname
        try:
            ipaddress.ip_address(host)
            raise ApiError(
                "url host must be a name, not an IP", code="media_host_not_allowed", status=403
            )
        except ValueError:
            pass
        if not _host_allowed(host, self._cfg.url_allowlist):
            raise ApiError(
                f"url host {host!r} is not in media.url_allowlist",
                code="media_host_not_allowed",
                status=403,
            )
        try:
            with (
                httpx.Client(
                    timeout=self._cfg.fetch_timeout_seconds, follow_redirects=False
                ) as client,
                client.stream("GET", url) as resp,
            ):
                if resp.status_code != 200:
                    raise UpstreamError(
                        f"media host returned {resp.status_code}", code="media_fetch_failed"
                    )
                declared = int(resp.headers.get("content-length") or 0)
                self._check_size(declared)
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    self._check_size(len(buf))
                ctype = resp.headers.get("content-type", "").split(";")[0].strip()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"media fetch failed: {exc}", code="media_fetch_failed") from exc
        ext = mimetypes.guess_extension(ctype) or "" if ctype else ""
        name = filename or Path(parts.path).name or "media"
        return safe_filename(name, ext), bytes(buf)

    def _check_size(self, n: int) -> None:
        if n > self._cfg.max_bytes:
            raise ApiError(
                f"media exceeds media.max_bytes ({self._cfg.max_bytes})",
                code="media_too_large",
                status=413,
            )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def content_type_for(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"
