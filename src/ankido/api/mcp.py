# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP at ``/mcp/p/{profile}``: the same operations as ``/v1``, as tools for LLM agents.

Streamable HTTP, stateless: protocol 2026-07-28 clients send self-contained requests, older
clients still get the ``initialize`` handshake but no session is kept between requests. One
:class:`MCPServer` serves every profile; the profile comes from the path.

Auth is Ankido's own: a static token from ``ankido token create`` or an OAuth access token
issued for exactly this URL (see :mod:`ankido.api.oauth`). ``tools/list`` shows only the tools
the caller's scopes allow, and every call checks the scope again.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any, Literal

from mcp.server.caching import CacheHint
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ankido import __version__, oauth
from ankido.api import v1
from ankido.api.deps import Principal, bearer_from
from ankido.api.oauth import public_base
from ankido.api.schemas import (
    MediaIn,
    MoveCardsRequest,
    NoteIn,
    NotesRequest,
    NoteUpdateIn,
    ReviewIn,
    ScheduleCardsRequest,
)
from ankido.collection import ops
from ankido.errors import ApiError, Forbidden, InternalError, Unauthorized
from ankido.logging import get_logger
from ankido.store import SCOPES
from ankido.supervisor import Supervisor
from ankido.worker import PRIORITY_READ

log = get_logger("ankido.mcp")

_STATE_KEY = "mcp_principal"

INSTRUCTIONS = """\
Ankido gives you one Anki collection (one profile). Anki owns scheduling: never compute due
dates or intervals yourself; submit grades and report what the server returns.

- Before adding notes, call list_note_types for the exact field names and list_decks for deck
  names (new decks are created on demand). Use search_notes to check what already exists.
- add_notes deduplicates on the first field by default (dedupe="skip").
- To edit a note, read it with search_notes(format="html") and write HTML back with
  update_notes, passing the note's `mod` as expected_mod. Plain text would drop formatting.
- reschedule_cards and move_cards take card_ids from search_notes or get_queue. Suspend or
  reschedule instead of grading Again when the schedule is wrong rather than the answer.
- delete_notes is permanent once synced. Only delete what the user explicitly asked for.
- For a review session use get_queue, show the question (q) only, wait for the user, reveal
  the answer (a), agree on a grade 1-4 (Again, Hard, Good, Easy), then submit_reviews.
- Give every new note and review a unique client_id so retries are harmless.
- Errors come back as JSON {"error": {"code", "message", "retryable", "details"}}; retry only
  when retryable is true.
"""

# ---- tool inputs (module level so the SDK can resolve their annotations) ----------------


class McpMedia(BaseModel):
    """Audio or picture for a note: base64 ``data`` with a ``filename``, or an https ``url``."""

    model_config = ConfigDict(extra="forbid")
    filename: str | None = Field(default=None, max_length=200)
    data: str | None = Field(default=None, description="base64 file content")
    url: str | None = Field(
        default=None, description="https URL; the host must be on the server's allowlist"
    )
    fields: list[str] = Field(
        default_factory=list, description="fields to append the media to (default: first field)"
    )


class McpNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str] = Field(description="field name -> value (HTML allowed)")
    client_id: str | None = Field(
        default=None, max_length=200, description="unique id; a retry with it is a no-op"
    )
    tags: list[str] = Field(default_factory=list)
    audio: list[McpMedia] = Field(default_factory=list)
    picture: list[McpMedia] = Field(default_factory=list)
    deck: str | None = Field(default=None, description="overrides the request's deck")
    model: str | None = Field(default=None, description="overrides the request's note type")


class McpNoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: int
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="field name -> new HTML value; fields not listed stay as they are",
    )
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    audio: list[McpMedia] = Field(default_factory=list)
    picture: list[McpMedia] = Field(default_factory=list)
    expected_mod: int | None = Field(
        default=None,
        description="`mod` from search_notes; if the note changed since, it is not updated"
        " (status error, code stale)",
    )
    allow_media_loss: bool = Field(
        default=False,
        description="allow a new field value to drop [sound:] or <img> tags the field had",
    )


class McpReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: int
    ease: int = Field(ge=1, le=4, description="1 Again, 2 Hard, 3 Good, 4 Easy")
    client_id: str | None = Field(
        default=None, max_length=200, description="unique id; a retry with it is a no-op"
    )
    answered_at: float | None = Field(
        default=None, description="epoch seconds when answered; default now"
    )
    time_ms: int = Field(default=0, ge=0, description="time the user took to answer")


# ---- helpers ----------------------------------------------------------------------------

_KLASS = {"read": "read", "add": "write", "review": "write", "sync": "sync", "delete": "write"}
_TOOL_SCOPES: dict[str, str] = {}


def _principal(ctx: Context) -> Principal:
    request: Request = ctx.request_context.request  # pyright: ignore[reportAssignmentType]
    return request.state.mcp_principal


def _error_result(exc: ApiError) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(exc.to_dict(), ensure_ascii=False))],
        is_error=True,
    )


def _allowed_scopes(p: Principal) -> set[str]:
    return {s for s in SCOPES if s != "admin" and p.token.allows(p.profile, s)}


async def _filter_tools(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    result = await call_next(ctx)
    if ctx.method == "tools/list" and isinstance(result, dict) and ctx.request is not None:
        p: Principal = ctx.request.state.mcp_principal  # pyright: ignore[reportAttributeAccessIssue]
        allowed = _allowed_scopes(p)
        result["tools"] = [t for t in result["tools"] if _TOOL_SCOPES.get(t["name"]) in allowed]
    return result


def _annotations(
    *, read_only: bool, idempotent: bool, open_world: bool = False, destructive: bool = False
) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


def build_server(sup: Supervisor) -> MCPServer:
    server = MCPServer(
        "ankido",
        version=__version__,
        instructions=INSTRUCTIONS,
        # The tool list depends on the caller's token: cacheable, but never shared.
        cache_hints={"tools/list": CacheHint(ttl_ms=300_000, scope="private")},
    )
    server.middleware.append(_filter_tools)

    def tool(
        scope: str, annotations: ToolAnnotations
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Register a tool that needs ``scope``; ApiErrors become ``isError`` results."""

        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            _TOOL_SCOPES[fn.__name__] = scope

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except ApiError as exc:
                    return _error_result(exc)
                except ValidationError as exc:
                    err = ApiError(
                        "invalid arguments",
                        code="validation_error",
                        status=422,
                        details={"errors": exc.errors(include_url=False, include_input=False)},
                    )
                    return _error_result(err)
                except Exception:
                    log.exception("mcp tool failed", fields={"tool": fn.__name__})
                    return _error_result(InternalError("internal error"))

            server.tool(annotations=annotations)(wrapper)
            return fn

        return deco

    def enter(ctx: Context, scope: str) -> Principal:
        p = _principal(ctx)
        sup.auth.require(p.token, p.profile, scope)
        sup.limiter.check(token_id=p.token.id, profile=p.profile, klass=_KLASS[scope])
        return p

    async def read(ctx: Context, name: str, fn: Callable[[Any], Any]) -> Any:
        p = enter(ctx, "read")
        return await run_in_threadpool(p.worker.submit, name, fn, priority=PRIORITY_READ)

    # ---- read ---------------------------------------------------------------------------

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def list_decks(ctx: Context) -> dict[str, Any]:
        """List decks with their hierarchy and new/learning/due counts."""
        return {"decks": await read(ctx, "decks", ops.get_decks)}

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def list_note_types(ctx: Context) -> dict[str, Any]:
        """List note types with their field names, in order. The first field is the one
        add_notes deduplicates on. Pass the name as `model` to add_notes."""
        return {"note_types": await read(ctx, "models", ops.get_models)}

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def search_notes(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description='Anki search syntax, e.g. "deck:Dutch huis", "tag:verbs",'
                ' "added:7", "front:kat*". Use "deck:*" for everything.',
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=ops.MAX_SEARCH_LIMIT)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        format: Annotated[
            Literal["text", "html"],
            Field(description='"html" returns fields as stored; use it before update_notes'),
        ] = "text",
    ) -> dict[str, Any]:
        """Find notes, newest first, with note_id, card_ids, tags and mod. Fields come back as
        plain text unless format="html". Page with offset / next_offset."""
        return await read(
            ctx,
            "search_notes",
            lambda s: ops.search_notes(s, query, limit=limit, offset=offset, render_mode=format),
        )

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def get_stats(
        ctx: Context,
        days: Annotated[int, Field(ge=1, le=3650, description="history window")] = 30,
    ) -> dict[str, Any]:
        """Reviews per day, today's count, per-deck due/new/learning counts, and when Anki's
        day rolls over (day_rollover_hour; the day does not start at midnight)."""
        return await read(ctx, "stats", lambda s: ops.get_stats(s, days))

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def get_queue(
        ctx: Context,
        decks: Annotated[
            list[str],
            Field(description="deck names in priority order; subdecks included; empty = all"),
        ] = [],  # noqa: B006  # the SDK reads the default for the JSON schema; never mutated
        kinds: Annotated[
            list[Literal["due", "new", "learning"]], Field(description="empty = all kinds")
        ] = [],  # noqa: B006
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Field(description="next_cursor from a previous call")] = None,
    ) -> dict[str, Any]:
        """The cards to study now, in Anki's order. Each card has the question (q), the answer
        (a) as plain text, and `next`: the interval each grade 1-4 would give. Show only q
        until the user has answered."""
        req = ops.QueueRequest(
            decks=decks,
            kinds=list(kinds) or list(ops.KINDS),
            limit=limit,
            cursor=cursor,
            fields="compact",
            render="text",
        )
        result = await read(ctx, "queue", lambda s: ops.get_queue(s, req))
        return result.to_dict()

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def list_tags(ctx: Context) -> dict[str, Any]:
        """Every tag in the collection. Check it before tagging to reuse existing tags."""
        return {"tags": await read(ctx, "tags", ops.list_tags)}

    @tool("read", _annotations(read_only=True, idempotent=True))
    async def sync_status(ctx: Context) -> dict[str, Any]:
        """When the collection last synced with AnkiWeb and whether a full sync is pending."""
        return await read(ctx, "sync_status", lambda s: s.sync_status())

    # ---- write --------------------------------------------------------------------------

    @tool("add", _annotations(read_only=False, idempotent=False, open_world=True))
    async def add_notes(
        ctx: Context,
        deck: Annotated[str, Field(description='e.g. "Dutch::Common"; created if missing')],
        notes: Annotated[list[McpNote], Field(min_length=1, max_length=100)],
        model: Annotated[str, Field(description="note type name from list_note_types")] = "Basic",
        tags: list[str] = [],  # noqa: B006
        dedupe: Annotated[
            Literal["skip", "update", "allow"],
            Field(description="on a matching first field: skip it, update it, or add anyway"),
        ] = "skip",
    ) -> dict[str, Any]:
        """Add notes. Per note the result is added, updated, skipped_duplicate or error.
        Media: pass base64 `data` + `filename`, or a `url` on the server's allowlist."""
        p = enter(ctx, "add")
        body = NotesRequest(
            deck=deck,
            model=model,
            tags=tags,
            dedupe=dedupe,
            notes=[
                NoteIn(
                    **n.model_dump(exclude={"audio", "picture"}),
                    audio=[MediaIn(**m.model_dump()) for m in n.audio],
                    picture=[MediaIn(**m.model_dump()) for m in n.picture],
                )
                for n in notes
            ],
        )
        return {"results": await run_in_threadpool(v1.add_notes, sup, p, body, via="mcp")}

    @tool("review", _annotations(read_only=False, idempotent=True))
    async def submit_reviews(
        ctx: Context,
        reviews: Annotated[list[McpReview], Field(min_length=1, max_length=200)],
    ) -> dict[str, Any]:
        """Grade cards from get_queue. Anki computes the new interval; each result says
        applied, duplicate (same client_id seen before) or rejected, plus the new due/interval."""
        p = enter(ctx, "review")
        specs = [ReviewIn(**r.model_dump()) for r in reviews]
        return {"results": await run_in_threadpool(v1.apply_reviews, sup, p, specs, via="mcp")}

    @tool("add", _annotations(read_only=False, idempotent=True, open_world=True))
    async def update_notes(
        ctx: Context,
        notes: Annotated[list[McpNoteUpdate], Field(min_length=1, max_length=ops.MAX_EDIT_ITEMS)],
    ) -> dict[str, Any]:
        """Edit existing notes: replace field values (HTML), add or remove tags, attach audio
        or pictures. Read the note with search_notes(format="html") first and pass its mod as
        expected_mod. Per note the result is updated, unchanged or error (e.g. stale,
        note_not_found, media_would_be_lost)."""
        p = enter(ctx, "add")
        items = [
            NoteUpdateIn(
                **n.model_dump(exclude={"audio", "picture"}),
                audio=[MediaIn(**m.model_dump()) for m in n.audio],
                picture=[MediaIn(**m.model_dump()) for m in n.picture],
            )
            for n in notes
        ]
        return {"results": await run_in_threadpool(v1.update_notes, sup, p, items, via="mcp")}

    @tool("delete", _annotations(read_only=False, idempotent=True, destructive=True))
    async def delete_notes(
        ctx: Context,
        note_ids: Annotated[list[int], Field(min_length=1, max_length=ops.MAX_EDIT_ITEMS)],
    ) -> dict[str, Any]:
        """Delete notes and all their cards and review history. Permanent on every device
        after the next sync; only delete what the user asked for. The server backs up the
        collection before the first delete in an hour (`backup`)."""
        p = enter(ctx, "delete")
        return await run_in_threadpool(v1.delete_notes, sup, p, note_ids, via="mcp")

    @tool("review", _annotations(read_only=False, idempotent=False))
    async def reschedule_cards(
        ctx: Context,
        card_ids: Annotated[list[int], Field(min_length=1, max_length=ops.MAX_EDIT_ITEMS)],
        action: Annotated[
            Literal["suspend", "unsuspend", "forget", "set_due"],
            Field(
                description="suspend: skip in reviews; unsuspend; forget: reset to new;"
                " set_due: due in `days`"
            ),
        ],
        days: Annotated[
            str | None,
            Field(
                description='set_due only: "0" today, "3" in 3 days, "3-7" random in range,'
                ' "7!" also set the interval to 7'
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Change cards' scheduling without recording a review, unlike submit_reviews.
        Each result is ok (with the new due/interval/queue) or not_found."""
        p = enter(ctx, "review")
        body = ScheduleCardsRequest(card_ids=card_ids, action=action, days=days)
        return {"results": await run_in_threadpool(v1.reschedule_cards, sup, p, body, via="mcp")}

    @tool("add", _annotations(read_only=False, idempotent=True))
    async def move_cards(
        ctx: Context,
        card_ids: Annotated[list[int], Field(min_length=1, max_length=ops.MAX_EDIT_ITEMS)],
        deck: Annotated[str, Field(description='e.g. "Dutch::Verbs"; created if missing')],
    ) -> dict[str, Any]:
        """Move cards to another deck. Each result is moved or not_found."""
        p = enter(ctx, "add")
        body = MoveCardsRequest(card_ids=card_ids, deck=deck)
        return await run_in_threadpool(v1.move_cards, sup, p, body, via="mcp")

    @tool("add", _annotations(read_only=False, idempotent=True))
    async def create_deck(
        ctx: Context,
        name: Annotated[
            str, Field(min_length=1, max_length=500, description='"Parent::Child" nests')
        ],
    ) -> dict[str, Any]:
        """Create an empty deck; returns the existing one if the name is taken. add_notes
        and move_cards create decks on their own, so this is only for empty decks."""
        p = enter(ctx, "add")
        return await run_in_threadpool(v1.create_deck, sup, p, name, via="mcp")

    @tool("sync", _annotations(read_only=False, idempotent=True, open_world=True))
    async def sync(
        ctx: Context,
        wait: Annotated[bool, Field(description="false = queue it and return at once")] = True,
    ) -> dict[str, Any]:
        """Incremental sync with AnkiWeb. Never does a full sync; if AnkiWeb demands one the
        error is sync_required_full and a human has to decide."""
        p = enter(ctx, "sync")
        return await run_in_threadpool(v1.run_sync, sup, p, wait=wait, via="mcp")

    # ---- prompt -------------------------------------------------------------------------

    @server.prompt(
        name="review_session",
        description="Quiz me on due cards, one at a time, and record my grades in Anki.",
    )
    def review_session(deck: str | None = None) -> str:
        where = f'the deck "{deck}"' if deck else "all decks"
        return f"""\
Run an Anki review session for {where} using the Ankido tools.

1. Call get_queue{f' with decks=["{deck}"]' if deck else ""} and limit=20.
   If it returns no cards, say so and show today's counts from get_stats.
2. For each card: show only the question (q). Wait for my answer. Do not reveal the answer
   (a) or hint at it before I reply.
3. Then show the answer and say whether I was right. Propose a grade: 1 Again (wrong),
   2 Hard, 3 Good, 4 Easy. I can override it. Mention the interval from `next` for that grade.
4. Submit grades with submit_reviews in batches of about 5, each with a unique client_id
   (e.g. "chat-<card_id>-<unix time>"). Never compute intervals yourself; Anki does that.
5. When the batch is done, fetch the next one with get_queue. Stop when I say so or the
   queue is empty, submit anything still pending, and end with a short summary.
6. If a card has a mistake, you may offer to fix it; ask first, then read it with
   search_notes(format="html") and call update_notes with expected_mod. If I keep failing a
   card, you may offer to suspend it with reschedule_cards; ask first. Never delete notes
   during a review.
"""

    return server


# ---- HTTP endpoint ----------------------------------------------------------------------


class McpEndpoint:
    """ASGI endpoint for ``/mcp/p/{profile}``: authenticate, then hand over to the SDK."""

    def __init__(self, sup: Supervisor) -> None:
        self.sup = sup
        self.server = build_server(sup)

    def lifespan(self) -> AbstractAsyncContextManager[None]:
        # A session manager runs once; build a fresh one for every app lifespan.
        self.server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            # Host checks are ours (server.public_url); reverse proxies send any Host.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        return self.server.session_manager.run()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        profile: str = scope["path_params"]["profile"]
        try:
            principal = await run_in_threadpool(self._authenticate, request, profile)
        except ApiError as exc:
            await self._error(request, profile, exc)(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state[_STATE_KEY] = principal
        state["token_id"] = principal.token.id
        await self.server.session_manager.handle_request(scope, receive, send)

    def _authenticate(self, request: Request, profile: str) -> Principal:
        raw = bearer_from(request)
        if raw is None:
            raise Unauthorized("missing bearer token")
        public = self.sup.config.server.public_url
        audience = oauth.resource_url(public, profile) if public else None
        token = self.sup.auth.authenticate(raw, audience=audience)
        worker = self.sup.worker(profile)  # only authenticated callers learn it exists
        if token.profile is not None and token.profile != profile:
            raise Forbidden("token is bound to another profile", details={"profile": profile})
        return Principal(token=token, worker=worker, profile=profile)

    def _error(self, request: Request, profile: str, exc: ApiError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.status == 401:
            challenge = self._challenge(request, profile, exc)
            if isinstance(challenge, ApiError):
                exc = challenge
                reason = challenge.message.replace('"', "'")
                headers["WWW-Authenticate"] = f'Bearer realm="ankido", error_description="{reason}"'

            else:
                headers["WWW-Authenticate"] = challenge
        elif exc.code == "rate_limited":
            headers["Retry-After"] = str(int(exc.details.get("retry_after_seconds", 1)) + 1)
        return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)

    def _challenge(self, request: Request, profile: str, exc: ApiError) -> str | ApiError:
        """``WWW-Authenticate`` pointing at the resource metadata, or why OAuth is unavailable.

        With no token at all, a missing or broken OAuth setup is the more useful error: it is
        what an MCP client needs fixed before it can sign in.
        """
        missing = exc.message == "missing bearer token"
        try:
            base = public_base(request, status=401)
        except ApiError as oauth_problem:
            if not missing:
                return 'Bearer realm="ankido", error="invalid_token"'
            oauth_problem.status = 401
            return oauth_problem
        metadata = f"{base}/.well-known/oauth-protected-resource/mcp/p/{profile}"
        scope = " ".join(oauth.DEFAULT_SCOPES)
        error = "" if missing else ', error="invalid_token"'
        return f'Bearer resource_metadata="{metadata}", scope="{scope}"{error}'
