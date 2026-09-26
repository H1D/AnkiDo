# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ankido.collection import media as media_mod
from ankido.collection.media import MediaResolver, content_type_for, safe_filename
from ankido.config import MediaConfig
from ankido.errors import ApiError, UpstreamError

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    d = tmp_path / "allowed"
    d.mkdir()
    return d


@pytest.fixture
def cfg(allowed_dir: Path) -> MediaConfig:
    return MediaConfig(
        url_allowlist=["media.example.com", "*.example.com"],
        path_allowlist=[allowed_dir],
        max_bytes=1024,
    )


@pytest.fixture
def resolver(cfg: MediaConfig) -> MediaResolver:
    return MediaResolver(cfg)


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Replace ``httpx.Client`` inside ankido.collection.media with a MockTransport client."""

    def install(handler: Handler) -> None:
        def factory(**kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        fake = SimpleNamespace(Client=factory, HTTPError=httpx.HTTPError)
        monkeypatch.setattr(media_mod, "httpx", fake)

    return install


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- data --------------------------------------------------------------------------------


def test_data_ok_and_filename_sanitised(resolver: MediaResolver) -> None:
    name, raw = resolver.resolve(
        filename="../../etc/huis (1).mp3", data=_b64(b"ID3abc"), path=None, url=None
    )
    assert name == "etc_huis _1_.mp3".replace("etc_", "") or name == "huis _1_.mp3"
    assert raw == b"ID3abc"


def test_data_bad_base64(resolver: MediaResolver) -> None:
    with pytest.raises(ApiError) as ei:
        resolver.resolve(filename="x.mp3", data="not base64!!", path=None, url=None)
    assert ei.value.code == "invalid_media"


def test_data_requires_filename(resolver: MediaResolver) -> None:
    with pytest.raises(ApiError, match="filename is required"):
        resolver.resolve(filename=None, data=_b64(b"x"), path=None, url=None)


def test_exactly_one_source_required(resolver: MediaResolver) -> None:
    with pytest.raises(ApiError, match="exactly one of"):
        resolver.resolve(filename="x", data=None, path=None, url=None)
    with pytest.raises(ApiError, match="exactly one of"):
        resolver.resolve(filename="x", data=_b64(b"x"), path="/tmp/x", url=None)


def test_data_size_cap(resolver: MediaResolver) -> None:
    with pytest.raises(ApiError) as ei:
        resolver.resolve(filename="big.mp3", data=_b64(b"x" * 1025), path=None, url=None)
    assert ei.value.code == "media_too_large" and ei.value.status == 413
    resolver.resolve(filename="ok.mp3", data=_b64(b"x" * 1024), path=None, url=None)


# ---- path --------------------------------------------------------------------------------


def test_path_inside_allowlist(resolver: MediaResolver, allowed_dir: Path) -> None:
    f = allowed_dir / "sub" / "pic.png"
    f.parent.mkdir()
    f.write_bytes(b"\x89PNG")
    name, raw = resolver.resolve(filename=None, data=None, path=str(f), url=None)
    assert name == "pic.png" and raw == b"\x89PNG"
    name, _ = resolver.resolve(filename="renamed.png", data=None, path=str(f), url=None)
    assert name == "renamed.png"


def test_path_outside_allowlist_forbidden(resolver: MediaResolver, tmp_path: Path) -> None:
    f = tmp_path / "secret.txt"
    f.write_bytes(b"nope")
    with pytest.raises(ApiError) as ei:
        resolver.resolve(filename=None, data=None, path=str(f), url=None)
    assert ei.value.code == "media_path_not_allowed" and ei.value.status == 403


def test_path_symlink_escaping_allowlist_forbidden(
    resolver: MediaResolver, allowed_dir: Path, tmp_path: Path
) -> None:
    target = tmp_path / "outside.bin"
    target.write_bytes(b"x")
    link = allowed_dir / "link.bin"
    link.symlink_to(target)
    with pytest.raises(ApiError) as ei:
        resolver.resolve(filename=None, data=None, path=str(link), url=None)
    assert ei.value.code == "media_path_not_allowed"


def test_path_relative_and_missing(resolver: MediaResolver, allowed_dir: Path) -> None:
    with pytest.raises(ApiError, match="path must be absolute"):
        resolver.resolve(filename=None, data=None, path="allowed/x.png", url=None)
    with pytest.raises(ApiError, match="path not found"):
        resolver.resolve(filename=None, data=None, path=str(allowed_dir / "nope.png"), url=None)


def test_path_size_cap(resolver: MediaResolver, allowed_dir: Path) -> None:
    f = allowed_dir / "big.bin"
    f.write_bytes(b"x" * 2000)
    with pytest.raises(ApiError) as ei:
        resolver.resolve(filename=None, data=None, path=str(f), url=None)
    assert ei.value.code == "media_too_large"


# ---- url ---------------------------------------------------------------------------------


def _ok_handler(body: bytes = b"ID3ok", ctype: str = "audio/mpeg") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    return handler


@pytest.mark.parametrize(
    "url",
    [
        "https://media.example.com/a/tone.mp3",
        "https://cdn.example.com/tone.mp3",  # *.example.com wildcard
        "http://EXAMPLE.COM./tone.mp3",  # bare domain, case, trailing dot
    ],
)
def test_url_allowed_hosts(
    resolver: MediaResolver, mock_http: Callable[[Handler], None], url: str
) -> None:
    mock_http(_ok_handler())
    name, raw = resolver.resolve(filename=None, data=None, path=None, url=url)
    assert name == "tone.mp3" and raw == b"ID3ok"


def test_url_filename_from_content_type_when_missing(
    resolver: MediaResolver, mock_http: Callable[[Handler], None]
) -> None:
    mock_http(_ok_handler(ctype="image/png; charset=binary"))
    name, _ = resolver.resolve(filename=None, data=None, path=None, url="https://example.com/")
    assert name == "media.png"
    name, _ = resolver.resolve(filename="given", data=None, path=None, url="https://example.com/x")
    assert name == "given.png"


def test_url_host_not_allowed(resolver: MediaResolver) -> None:
    with pytest.raises(ApiError) as ei:
        resolver.resolve(filename=None, data=None, path=None, url="https://evil.org/x.mp3")
    assert ei.value.code == "media_host_not_allowed" and ei.value.status == 403
    with pytest.raises(ApiError) as ei:  # suffix trick must not match the wildcard
        resolver.resolve(filename=None, data=None, path=None, url="https://notexample.com/x")
    assert ei.value.code == "media_host_not_allowed"


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.mp3", "http://[::1]/x.mp3"])
def test_url_ip_hosts_rejected(resolver: MediaResolver, url: str) -> None:
    with pytest.raises(ApiError, match="must be a name, not an IP") as ei:
        resolver.resolve(filename=None, data=None, path=None, url=url)
    assert ei.value.code == "media_host_not_allowed"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "https:///x"])
def test_url_scheme_rejected(resolver: MediaResolver, url: str) -> None:
    with pytest.raises(ApiError, match="url must be http"):
        resolver.resolve(filename=None, data=None, path=None, url=url)


def test_url_upstream_status_and_redirects_fail(
    resolver: MediaResolver, mock_http: Callable[[Handler], None]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://evil.org/x"})

    mock_http(handler)
    with pytest.raises(UpstreamError) as ei:
        resolver.resolve(filename=None, data=None, path=None, url="https://example.com/missing")
    assert ei.value.code == "media_fetch_failed" and ei.value.retryable is True
    with pytest.raises(UpstreamError, match="returned 302"):
        resolver.resolve(filename=None, data=None, path=None, url="https://example.com/redir")


def test_url_transport_error(resolver: MediaResolver, mock_http: Callable[[Handler], None]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    mock_http(handler)
    with pytest.raises(UpstreamError, match="media fetch failed"):
        resolver.resolve(filename=None, data=None, path=None, url="https://example.com/x")


def test_url_size_cap_declared_and_streamed(
    resolver: MediaResolver, mock_http: Callable[[Handler], None]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/declared":
            return httpx.Response(200, content=b"x" * 2000)  # content-length: 2000
        return httpx.Response(200, content=iter([b"x" * 600, b"x" * 600]))  # chunked

    mock_http(handler)
    for path in ("/declared", "/streamed"):
        with pytest.raises(ApiError) as ei:
            resolver.resolve(filename=None, data=None, path=None, url=f"https://example.com{path}")
        assert ei.value.code == "media_too_large", path


# ---- helpers -----------------------------------------------------------------------------


def test_safe_filename() -> None:
    assert safe_filename("../../x/y.mp3") == "y.mp3"
    assert safe_filename("héllo wörld?.png") == "h_llo w_rld_.png"
    assert safe_filename("...") == "media"
    assert safe_filename("") == "media"
    assert safe_filename("noext", ".mp3") == "noext.mp3"
    assert safe_filename("has.ext", ".mp3") == "has.ext"
    long = "a" * 200 + ".mp3"
    out = safe_filename(long)
    assert len(out) <= 120 and out.endswith(".mp3")
    assert len(safe_filename("b" * 200)) == 120


def test_content_type_for() -> None:
    assert content_type_for("x.mp3") == "audio/mpeg"
    assert content_type_for("x.png") == "image/png"
    assert content_type_for("x.unknownext") == "application/octet-stream"
