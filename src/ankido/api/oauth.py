# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth endpoints for MCP clients that cannot send a static bearer header (claude.ai).

* ``/.well-known/oauth-protected-resource/mcp/p/{profile}`` — RFC 9728 resource metadata,
* ``/.well-known/oauth-authorization-server`` — RFC 8414 server metadata,
* ``/oauth/authorize`` — the consent page (GET) and its form (POST),
* ``/oauth/token``, ``/oauth/register``, ``/oauth/revoke``.

All of it needs ``server.public_url``. Without it, or when requests arrive under another host,
every endpoint answers with an error that says exactly what to change.
"""

from __future__ import annotations

import base64
import html
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ankido import oauth
from ankido.api.deps import client_ip, is_trusted_proxy, supervisor_of
from ankido.config import Config
from ankido.errors import ApiError, NotFound, RateLimited
from ankido.oauth import OAuthError, PendingAuthorization
from ankido.store import IssuedTokens, OAuthClient, TokenRecord

router = APIRouter()

DOCS = "https://github.com/H1D/AnkiDo/blob/main/docs/mcp.md"


def _csrf_cookie(pending: PendingAuthorization) -> str:
    # One cookie per consent request, so two open consent tabs do not clobber each other.
    return f"ankido_csrf_{pending.request_id[:12]}"


# ---- public_url checks ------------------------------------------------------------------


def not_configured_error(status: int = 404) -> ApiError:
    return ApiError(
        "OAuth for MCP is not configured on this server: server.public_url is not set",
        code="oauth_not_configured",
        status=status,
        details={
            "fix": [
                "set server.public_url in ankido.yaml to the external HTTPS origin clients use,"
                " e.g. https://anki.example.com (no path)",
                "restart ankido",
                "or skip OAuth: create a token with `ankido token create --profile <name>"
                " --scopes read,add,review` and send it as 'Authorization: Bearer <token>'",
            ],
            "docs": f"{DOCS}#oauth",
        },
    )


def _seen_origin(request: Request, config: Config) -> str:
    host = request.headers.get("host", "")
    scheme = request.url.scheme
    peer = request.client.host if request.client else None
    if peer and is_trusted_proxy(peer, config.server.trusted_proxies):
        forwarded = request.headers.get("x-forwarded-host")
        if forwarded:
            host = forwarded.split(",")[0].strip()
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            scheme = proto.split(",")[0].strip()
    return f"{scheme}://{host}"


def _netloc(origin: str) -> str:
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    if ":" in host:
        host = f"[{host}]"
    return host if port in (None, default) else f"{host}:{port}"


def public_base(request: Request, *, status: int = 404) -> str:
    """The configured public origin, or an actionable error explaining why OAuth can't work."""
    config: Config = request.app.state.config
    public = config.server.public_url
    if public is None:
        raise not_configured_error(status)
    if oauth.oauth_status(config) == "insecure":
        raise ApiError(
            "server.public_url must use https; MCP clients refuse OAuth over plain http",
            code="oauth_public_url_mismatch",
            status=400,
            details={
                "expected": "https://…",
                "configured": public,
                "fix": [
                    "put ankido behind a TLS reverse proxy and set server.public_url to its"
                    " https:// origin",
                ],
                "docs": f"{DOCS}#oauth",
            },
        )
    seen = _seen_origin(request, config)
    if _netloc(seen) != _netloc(public):
        raise ApiError(
            f"request arrived for host {_netloc(seen)!r} but server.public_url is {public!r}",
            code="oauth_public_url_mismatch",
            status=400,
            details={
                "expected": public,
                "seen": seen,
                "fix": [
                    f"connect MCP clients to {public}/mcp/p/<profile>, not to another address",
                    "if this is the right address, set server.public_url to it and restart",
                    "behind a reverse proxy, forward the original Host header (Caddy does by"
                    " default; nginx: proxy_set_header Host $host) or list the proxy in"
                    " server.trusted_proxies so X-Forwarded-Host is honored",
                ],
                "docs": f"{DOCS}#oauth",
            },
        )
    return public


# ---- metadata ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource/mcp/p/{profile}")
def protected_resource(profile: str, request: Request) -> JSONResponse:
    base = public_base(request)
    config: Config = request.app.state.config
    if profile not in config.profiles:
        raise NotFound(f"profile {profile!r} not found", code="profile_not_found")
    return JSONResponse(
        {
            "resource": oauth.resource_url(base, profile),
            "authorization_servers": [base],
            "scopes_supported": list(oauth.OAUTH_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": f"Ankido ({profile})",
            "resource_documentation": DOCS,
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/.well-known/oauth-authorization-server")
def authorization_server(request: Request) -> JSONResponse:
    base = public_base(request)
    methods = ["none", "client_secret_post", "client_secret_basic"]
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": methods,
            "revocation_endpoint_auth_methods_supported": methods,
            "scopes_supported": list(oauth.OAUTH_SCOPES),
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "service_documentation": DOCS,
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


# ---- authorize --------------------------------------------------------------------------


def _limit(request: Request) -> None:
    sup = supervisor_of(request)
    sup.limiter.check_key(client_ip(request) or "unknown", klass="oauth")


def _with_params(uri: str, params: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = parts.query + ("&" if parts.query else "") + urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _redirect(uri: str, params: dict[str, str | None], *, status: int = 302) -> RedirectResponse:
    """302 from GET; 303 from the consent POST so no browser re-posts the pasted token."""
    clean = {k: v for k, v in params.items() if v is not None}
    return RedirectResponse(_with_params(uri, clean), status_code=status)


@router.get("/oauth/authorize")
async def authorize_page(request: Request) -> Response:
    base = public_base(request)
    q = request.query_params
    registry: oauth.ClientRegistry = request.app.state.oauth_clients
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    # Until client and redirect_uri check out, errors go on the page, never to a redirect.
    if not client_id:
        return _error_page("The request has no client_id.", status=400)
    try:
        # Loading the page can make Ankido fetch a client metadata document; keep that cheap.
        _limit(request)
    except RateLimited as exc:
        wait = exc.details.get("retry_after_seconds", 1)
        return _error_page(f"Too many requests. Wait {wait:.0f} s and reload.", status=429)
    try:
        client = await run_in_threadpool(registry.get, client_id)
    except OAuthError as exc:
        return _error_page(f"Unknown or invalid client: {exc.description}", status=400)
    if not redirect_uri or not oauth.redirect_uri_matches(client.redirect_uris, redirect_uri):
        return _error_page("The redirect_uri is not registered for this client.", status=400)

    state = q.get("state")

    def fail(error: str, description: str) -> RedirectResponse:
        return _redirect(
            redirect_uri,
            {"error": error, "error_description": description, "state": state, "iss": base},
        )

    if q.get("response_type") != "code":
        return fail("unsupported_response_type", "only response_type=code is supported")
    challenge = q.get("code_challenge", "")
    if not challenge or q.get("code_challenge_method") != "S256":
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
    resource = q.get("resource", "").rstrip("/")
    config: Config = request.app.state.config
    profile = oauth.profile_from_resource(base, resource, config.profiles)
    if profile is None:
        return fail(
            "invalid_target",
            f"resource must be {base}/mcp/p/<profile> for a configured profile",
        )
    requested = oauth.parse_scope(q.get("scope")) or list(oauth.DEFAULT_SCOPES)
    pending = sup_pending(request).add(
        client=client,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        resource=oauth.resource_url(base, profile),
        profile=profile,
        requested=requested,
    )
    return _consent_page(request, pending, checked=requested)


def sup_pending(request: Request) -> oauth.PendingStore:
    return request.app.state.oauth_pending


@router.post("/oauth/authorize")
async def authorize_submit(request: Request) -> Response:
    base = public_base(request)
    form = _parse_form(await request.body())
    pending = sup_pending(request).get(form.get("request_id", [""])[0])
    if pending is None:
        return _error_page(
            "This sign-in request has expired. Start again from your MCP client.", status=400
        )
    csrf = form.get("csrf", [""])[0]
    cookie = request.cookies.get(_csrf_cookie(pending), "")
    if not secrets.compare_digest(csrf, cookie) or not secrets.compare_digest(csrf, pending.csrf):
        return _error_page("The form could not be verified. Reload the page.", status=400)
    checked = [s for s in oauth.OAUTH_SCOPES if s in form.get("scope", [])]

    if form.get("action", [""])[0] != "approve":
        sup_pending(request).pop(pending.request_id)
        return _redirect(
            pending.redirect_uri,
            {
                "error": "access_denied",
                "error_description": "the user denied access",
                "state": pending.state,
                "iss": base,
            },
            status=303,
        )

    try:
        _limit(request)
    except RateLimited as exc:
        wait = exc.details.get("retry_after_seconds", 1)
        return _consent_page(
            request, pending, checked, error=f"Too many attempts. Wait {wait:.0f} s.", status=429
        )

    sup = supervisor_of(request)
    raw = form.get("token", [""])[0].strip()
    parent = await run_in_threadpool(sup.store.verify_token, raw) if raw else None
    if parent is None or (parent.profile is not None and parent.profile != pending.profile):
        sup.store.audit(
            token_id=parent.id if parent else None,
            profile=pending.profile,
            action="oauth_consent",
            outcome="bad_token",
            detail="via=oauth",
        )
        return _consent_page(
            request,
            pending,
            checked,
            error=f"That is not a valid Ankido token for profile “{pending.profile}”.",
            status=400,
        )
    allowed = oauth.grantable_scopes(parent)
    too_wide = [s for s in checked if s not in allowed]
    if too_wide:
        return _consent_page(
            request,
            pending,
            checked,
            error=f"Your token cannot grant: {', '.join(too_wide)}.",
            status=400,
        )
    if not checked:
        return _consent_page(
            request, pending, checked, error="Pick at least one scope.", status=400
        )

    code = sup.store.put_oauth_code(
        client_id=pending.client.client_id,
        profile=pending.profile,
        scopes=frozenset(checked),
        redirect_uri=pending.redirect_uri,
        challenge=pending.challenge,
        resource=pending.resource,
        parent_id=parent.id,
        ttl_seconds=oauth.CODE_TTL,
    )
    sup_pending(request).pop(pending.request_id)
    sup.store.audit(
        token_id=parent.id,
        profile=pending.profile,
        action="oauth_consent",
        outcome="granted",
        count=len(checked),
        detail=f"via=oauth client={oauth.client_label(pending.client)[:60]}"
        f" scopes={','.join(checked)}",
    )
    return _redirect(
        pending.redirect_uri, {"code": code, "state": pending.state, "iss": base}, status=303
    )


# ---- token ------------------------------------------------------------------------------


def _oauth_json(body: dict[str, Any], status: int = 200) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="ankido"'
    return JSONResponse(body, status_code=status, headers=headers)


def _client_credentials(request: Request, form: dict[str, list[str]]) -> tuple[str, str | None]:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "basic" and value:
        try:
            decoded = base64.b64decode(value.strip()).decode()
        except ValueError as exc:
            raise OAuthError("invalid_client", "malformed Basic credentials", status=401) from exc
        cid, _, secret = decoded.partition(":")
        return unquote(cid), unquote(secret)
    return form.get("client_id", [""])[0], form.get("client_secret", [None])[0]


async def _authenticate_client(request: Request, form: dict[str, list[str]]) -> OAuthClient:
    client_id, secret = _client_credentials(request, form)
    if not client_id:
        raise OAuthError("invalid_client", "client_id is required", status=401)
    registry: oauth.ClientRegistry = request.app.state.oauth_clients
    client = await run_in_threadpool(registry.get, client_id)
    if client.auth_method != "none" and not client.check_secret(secret):
        raise OAuthError("invalid_client", "client authentication failed", status=401)
    if client.kind == "dcr":
        supervisor_of(request).store.touch_oauth_client(client.client_id)
    return client


def _parse_form(body: bytes) -> dict[str, list[str]]:
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return {}


def _token_response(issued: IssuedTokens) -> JSONResponse:
    return _oauth_json(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": " ".join(s for s in oauth.OAUTH_SCOPES if s in issued.record.scopes),
        }
    )


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    public_base(request)
    try:
        _limit(request)
        form = _parse_form(await request.body())
        client = await _authenticate_client(request, form)
        grant_type = form.get("grant_type", [""])[0]
        if grant_type == "authorization_code":
            return await run_in_threadpool(_exchange_code, request, form, client)
        if grant_type == "refresh_token":
            return await run_in_threadpool(_refresh, request, form, client)
        raise OAuthError("unsupported_grant_type", f"grant_type {grant_type!r} is not supported")
    except OAuthError as exc:
        return _oauth_json(exc.to_dict(), exc.status)
    except RateLimited as exc:
        return _oauth_json({"error": "slow_down", "error_description": exc.message}, 429)


def _exchange_code(
    request: Request, form: dict[str, list[str]], client: OAuthClient
) -> JSONResponse:
    sup = supervisor_of(request)
    code = sup.store.take_oauth_code(form.get("code", [""])[0])
    if code is None or code.client_id != client.client_id:
        raise OAuthError("invalid_grant", "authorization code is invalid, used or expired")
    if form.get("redirect_uri", [""])[0] != code.redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
    if not oauth.verify_pkce(form.get("code_verifier", [""])[0], code.challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    resource = form.get("resource", [None])[0]
    if resource is not None and resource.rstrip("/") != code.resource:
        raise OAuthError("invalid_target", "resource does not match the authorization request")
    parent = sup.store.get_token(code.parent_id)
    if parent is None or not parent.is_valid():
        raise OAuthError("invalid_grant", "the token used to approve this client was revoked")
    scopes = code.scopes & oauth.grantable_scopes(parent)
    issued = sup.store.create_oauth_grant(
        profile=code.profile,
        scopes=scopes,
        name=f"oauth:{oauth.client_label(client)}"[:60],
        parent_id=parent.id,
        client_id=client.client_id,
        resource=code.resource,
        access_ttl=oauth.ACCESS_TTL,
        expires_at=_grant_expiry(parent),
    )
    sup.store.audit(
        token_id=issued.record.id,
        profile=code.profile,
        action="oauth_token",
        outcome="issued",
        detail=f"via=oauth parent={parent.id}",
    )
    return _token_response(issued)


def _grant_expiry(parent: TokenRecord) -> int:
    limit = int(time.time()) + oauth.REFRESH_TTL
    return min(limit, parent.expires_at) if parent.expires_at else limit


def _refresh(request: Request, form: dict[str, list[str]], client: OAuthClient) -> JSONResponse:
    sup = supervisor_of(request)
    raw = form.get("refresh_token", [""])[0]
    issued = sup.store.rotate_oauth_refresh(raw, client.client_id, oauth.ACCESS_TTL)
    if issued is None:
        raise OAuthError("invalid_grant", "refresh token is invalid, rotated, revoked or expired")
    parent = sup.store.get_token(issued.record.parent_id or "")
    if parent is None or not parent.is_valid():
        sup.store.revoke_token(issued.record.id)
        raise OAuthError("invalid_grant", "the token used to approve this client was revoked")
    # Sliding window: every refresh extends the grant, never past the approving token's expiry.
    sup.store.set_token_expiry(issued.record.id, _grant_expiry(parent))
    return _token_response(issued)


# ---- register / revoke ------------------------------------------------------------------


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    public_base(request)
    try:
        _limit(request)
    except RateLimited as exc:
        return _oauth_json({"error": "slow_down", "error_description": exc.message}, 429)
    try:
        body = json.loads(await request.body())
    except ValueError:
        return _oauth_json(
            {"error": "invalid_client_metadata", "error_description": "body must be JSON"}, 400
        )
    if not isinstance(body, dict):
        return _oauth_json(
            {"error": "invalid_client_metadata", "error_description": "body must be an object"},
            400,
        )
    registry: oauth.ClientRegistry = request.app.state.oauth_clients
    try:
        out = await run_in_threadpool(registry.register, body)
    except OAuthError as exc:
        return _oauth_json(exc.to_dict(), exc.status)
    return _oauth_json(out, 201)


@router.post("/oauth/revoke")
async def revoke(request: Request) -> Response:
    public_base(request)
    form = _parse_form(await request.body())
    try:
        _limit(request)
        client = await _authenticate_client(request, form)
    except OAuthError as exc:
        return _oauth_json(exc.to_dict(), exc.status)
    except RateLimited as exc:
        return _oauth_json({"error": "slow_down", "error_description": exc.message}, 429)
    sup = supervisor_of(request)
    revoked = sup.store.revoke_oauth_secret(form.get("token", [""])[0], client.client_id)
    if revoked:
        sup.store.audit(
            token_id=revoked, profile=None, action="oauth_revoke", outcome="ok", detail="via=oauth"
        )
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


# ---- HTML -------------------------------------------------------------------------------

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:;"
    " frame-ancestors 'none'; base-uri 'none'"
)

_STYLE = """
body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;
color:#1d1d1f;background:#fafafa}
h1{font-size:1.4rem;margin:0 0 1rem}
.box{background:#fff;border:1px solid #ddd;border-radius:10px;padding:1.25rem 1.5rem}
code{background:#f0f0f0;padding:.1em .3em;border-radius:4px}
label{display:block;margin:.35rem 0}
input[type=password]{width:100%;box-sizing:border-box;padding:.5rem;font:inherit;
border:1px solid #bbb;border-radius:6px}
.err{background:#fdecea;border:1px solid #f5c2c0;padding:.6rem .8rem;border-radius:6px}
.row{display:flex;gap:.75rem;margin-top:1.25rem}
button{font:inherit;padding:.5rem 1.1rem;border-radius:6px;border:1px solid #888;background:#fff}
button.primary{background:#1d1d1f;color:#fff;border-color:#1d1d1f}
small{color:#666}
"""

_SCOPE_TEXT = {
    "read": "Read decks, notes, stats and the review queue",
    "add": "Add notes (and their audio or pictures)",
    "review": "Submit review grades",
    "sync": "Start a sync with AnkiWeb",
}


def _page(body: str, *, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Ankido</title><style>{_STYLE}</style></head><body>{body}</body></html>"
    )
    return HTMLResponse(
        doc,
        status_code=status,
        headers={
            "Content-Security-Policy": _CSP,
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _error_page(message: str, *, status: int) -> HTMLResponse:
    return _page(
        f'<h1>Ankido</h1><div class="box"><p class="err">{html.escape(message)}</p></div>',
        status=status,
    )


def _consent_page(
    request: Request,
    pending: PendingAuthorization,
    checked: list[str],
    *,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    e = html.escape
    client = pending.client
    label = oauth.client_label(client)
    where = urlsplit(pending.redirect_uri)
    dest = where.netloc or f"{where.scheme}:"
    boxes = "".join(
        f'<label><input type=checkbox name=scope value="{s}"{" checked" if s in checked else ""}>'
        f" <code>{s}</code> — {e(_SCOPE_TEXT[s])}</label>"
        for s in oauth.OAUTH_SCOPES
    )
    err = f'<p class="err">{e(error)}</p>' if error else ""
    body = f"""
<h1>Connect {e(label)} to Ankido</h1>
<div class="box">
{err}
<p><b>{e(label)}</b> wants access to the profile <b>{e(pending.profile)}</b>.
After you approve, you will be sent to <code>{e(dest)}</code>.</p>
<form method=post action="/oauth/authorize">
<input type=hidden name=request_id value="{e(pending.request_id)}">
<input type=hidden name=csrf value="{e(pending.csrf)}">
<p>{boxes}</p>
<label for=token>Paste an Ankido token for <b>{e(pending.profile)}</b></label>
<input type=password id=token name=token autocomplete=off required>
<p><small>Create one on the server with
<code>ankido token create --profile {e(pending.profile)} --scopes read,add,review</code>.
The client gets its own token with at most these scopes; revoking the pasted token later
disconnects it too.</small></p>
<div class="row">
<button class="primary" name=action value=approve>Approve</button>
<button name=action value=deny formnovalidate>Deny</button>
</div>
</form>
</div>"""
    resp = _page(body, status=status)
    public = request.app.state.config.server.public_url or ""
    resp.set_cookie(
        _csrf_cookie(pending),
        pending.csrf,
        max_age=oauth.PENDING_TTL,
        path="/oauth",
        secure=public.startswith("https://"),
        httponly=True,
        samesite="strict",
    )
    return resp
