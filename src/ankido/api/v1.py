# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``/v1`` REST surface."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from ankido.api.deps import Principal, authorize, authorize_admin, supervisor_of
from ankido.api.schemas import (
    CreateDeckRequest,
    DeleteNotesRequest,
    ExchangeRequest,
    MediaIn,
    MoveCardsRequest,
    NotesRequest,
    NoteUpdateIn,
    ReviewIn,
    ReviewsRequest,
    ScheduleCardsRequest,
    SyncRequest,
    UpdateNotesRequest,
    WantIn,
)
from ankido.collection import ops
from ankido.collection.media import content_type_for
from ankido.collection.session import Session
from ankido.errors import ApiError, NotFound
from ankido.supervisor import Supervisor
from ankido.worker import PRIORITY_READ, PRIORITY_SYNC, PRIORITY_WRITE

router = APIRouter(prefix="/v1")


def _etag_response(request: Request, payload: dict[str, Any], *, max_age: int = 0) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": f"private, max-age={max_age}"}
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def audit(
    sup: Supervisor,
    p: Principal,
    action: str,
    outcome: str,
    count: int = 0,
    *,
    via: str | None,
    detail: str | None = None,
) -> None:
    parts = [f"via={via}"] if via else []
    if detail:
        parts.append(detail)
    sup.store.audit(
        token_id=p.token.id,
        profile=p.profile,
        action=action,
        outcome=outcome,
        count=count,
        detail=" ".join(parts) or None,
    )


# ---- notes ------------------------------------------------------------------------------


@router.post("/p/{profile}/notes")
def post_notes(profile: str, body: NotesRequest, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "add", klass="write")
    return {"results": add_notes(supervisor_of(request), p, body, via=None)}


def add_notes(
    sup: Supervisor, p: Principal, body: NotesRequest, *, via: str | None
) -> list[dict[str, Any]]:
    """``POST notes`` minus HTTP; the MCP ``add_notes`` tool runs this too."""
    req = ops.AddNotesRequest(
        deck=body.deck,
        model=body.model,
        tags=body.tags,
        dedupe=body.dedupe,
        notes=[
            ops.NoteSpec(
                fields=n.fields,
                client_id=n.client_id,
                tags=n.tags,
                deck=n.deck,
                model=n.model,
                audio=_media_specs(n.audio, "audio"),
                picture=_media_specs(n.picture, "picture"),
            )
            for n in body.notes
        ],
    )
    results = p.worker.submit(
        "notes",
        lambda s: ops.add_notes(s, sup.store, sup.media, req),
        priority=PRIORITY_WRITE,
    )
    changed = sum(r["status"] in ("added", "updated") for r in results)
    if changed:
        p.worker.note_write()
    audit(sup, p, "notes", "ok", len(results), via=via)
    return results


@router.patch("/p/{profile}/notes")
def patch_notes(profile: str, body: UpdateNotesRequest, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "add", klass="write")
    return {"results": update_notes(supervisor_of(request), p, body.notes, via=None)}


def _media_specs(items: list[MediaIn], kind: Literal["audio", "picture"]) -> list[ops.MediaSpec]:
    return [ops.MediaSpec(**m.model_dump(), kind=kind) for m in items]


def update_notes(
    sup: Supervisor, p: Principal, notes: list[NoteUpdateIn], *, via: str | None
) -> list[dict[str, Any]]:
    """``PATCH notes`` minus HTTP; the MCP ``update_notes`` tool runs this too."""
    updates = [
        ops.NoteUpdate(
            **n.model_dump(exclude={"audio", "picture"}),
            audio=_media_specs(n.audio, "audio"),
            picture=_media_specs(n.picture, "picture"),
        )
        for n in notes
    ]
    results = p.worker.submit(
        "update_notes",
        lambda s: ops.update_notes(s, sup.media, updates),
        priority=PRIORITY_WRITE,
    )
    changed = sum(r["status"] == "updated" for r in results)
    if changed:
        p.worker.note_write()
    audit(sup, p, "update_notes", "ok", changed, via=via)
    return results


@router.post("/p/{profile}/notes/delete")
def post_notes_delete(profile: str, body: DeleteNotesRequest, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "delete", klass="write")
    return delete_notes(supervisor_of(request), p, body.note_ids, via=None)


def delete_notes(
    sup: Supervisor, p: Principal, note_ids: list[int], *, via: str | None
) -> dict[str, Any]:
    """``POST notes/delete`` minus HTTP; the MCP ``delete_notes`` tool runs this too."""
    # The first delete in an hour takes a backup, which can take a while on a big collection.
    result = p.worker.submit(
        "delete_notes",
        lambda s: ops.delete_notes(s, note_ids),
        priority=PRIORITY_WRITE,
        timeout=300,
    )
    deleted = [str(r["note_id"]) for r in result["results"] if r["status"] == "deleted"]
    if deleted:
        p.worker.note_write()
    detail = f"note_ids={','.join(deleted)}" if deleted else None
    if result["backup"]:
        detail = f"{detail} backup={result['backup']}"
    audit(sup, p, "delete_notes", "ok", len(deleted), via=via, detail=detail)
    return result


@router.get("/p/{profile}/tags")
def get_tags(profile: str, request: Request) -> Response:
    p = authorize(request, profile, "read", klass="read")
    result = p.worker.submit("tags", ops.list_tags, priority=PRIORITY_READ)
    return _etag_response(request, {"tags": result})


# ---- cards and decks --------------------------------------------------------------------


@router.post("/p/{profile}/cards/schedule")
def post_cards_schedule(
    profile: str, body: ScheduleCardsRequest, request: Request
) -> dict[str, Any]:
    p = authorize(request, profile, "review", klass="write")
    return {"results": reschedule_cards(supervisor_of(request), p, body, via=None)}


def reschedule_cards(
    sup: Supervisor, p: Principal, body: ScheduleCardsRequest, *, via: str | None
) -> list[dict[str, Any]]:
    """``POST cards/schedule`` minus HTTP; the MCP ``reschedule_cards`` tool runs this too."""
    results = p.worker.submit(
        "reschedule_cards",
        lambda s: ops.reschedule_cards(s, body.card_ids, body.action, body.days),
        priority=PRIORITY_WRITE,
    )
    done = sum(r["status"] == "ok" for r in results)
    if done:
        p.worker.note_write()
    audit(sup, p, f"cards_{body.action}", "ok", done, via=via)
    return results


@router.post("/p/{profile}/cards/move")
def post_cards_move(profile: str, body: MoveCardsRequest, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "add", klass="write")
    return move_cards(supervisor_of(request), p, body, via=None)


def move_cards(
    sup: Supervisor, p: Principal, body: MoveCardsRequest, *, via: str | None
) -> dict[str, Any]:
    """``POST cards/move`` minus HTTP; the MCP ``move_cards`` tool runs this too."""
    result = p.worker.submit(
        "move_cards",
        lambda s: ops.move_cards(s, body.card_ids, body.deck),
        priority=PRIORITY_WRITE,
    )
    moved = sum(r["status"] == "moved" for r in result["results"])
    p.worker.note_write()  # the deck may have been created even if no card moved
    audit(sup, p, "move_cards", "ok", moved, via=via)
    return result


@router.post("/p/{profile}/decks")
def post_decks(profile: str, body: CreateDeckRequest, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "add", klass="write")
    return create_deck(supervisor_of(request), p, body.name, via=None)


def create_deck(sup: Supervisor, p: Principal, name: str, *, via: str | None) -> dict[str, Any]:
    """``POST decks`` minus HTTP; the MCP ``create_deck`` tool runs this too."""
    result = p.worker.submit(
        "create_deck", lambda s: ops.create_deck(s, name), priority=PRIORITY_WRITE
    )
    if result["created"]:
        p.worker.note_write()
    audit(sup, p, "create_deck", "ok", int(result["created"]), via=via)
    return result


# ---- reviews ----------------------------------------------------------------------------


def _apply_reviews(request: Request, p: Principal, reviews: list[Any]) -> list[dict[str, Any]]:
    return apply_reviews(supervisor_of(request), p, reviews, via=None)


def apply_reviews(
    sup: Supervisor, p: Principal, reviews: list[ReviewIn], *, via: str | None
) -> list[dict[str, Any]]:
    """``POST reviews`` minus HTTP; the MCP ``submit_reviews`` tool runs this too."""
    specs = [ops.ReviewSpec(**r.model_dump()) for r in reviews]
    received = time.time()
    results = p.worker.submit(
        "reviews",
        lambda s: ops.answer_reviews(s, sup.store, specs, received_at=received),
        priority=PRIORITY_WRITE,
    )
    applied = sum(r["status"] == "applied" for r in results)
    if applied:
        p.worker.note_write()
    audit(sup, p, "reviews", "ok", applied, via=via)
    return results


@router.post("/p/{profile}/reviews")
def post_reviews(profile: str, body: ReviewsRequest, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "review", klass="write")
    return {"results": _apply_reviews(request, p, body.reviews)}


# ---- queue ------------------------------------------------------------------------------


def _queue_request(want: WantIn, cursor: str | None) -> ops.QueueRequest:
    return ops.QueueRequest(
        decks=want.decks,
        kinds=want.kinds,
        limit=want.limit,
        max_new_per_day=want.max_new_per_day,
        cursor=cursor,
        fields=want.fields,
        render=want.render,
    )


@router.get("/p/{profile}/queue")
def get_queue(
    profile: str,
    request: Request,
    decks: list[str] = Query(default=[]),
    kinds: list[str] = Query(default=[]),
    limit: int = Query(default=60, ge=1, le=1000),
    max_new_per_day: int | None = Query(default=None, ge=0),
    cursor: str | None = None,
    fields: str = Query(default="compact", pattern="^(compact|full)$"),
    render: str = Query(default="text", pattern="^(text|html)$"),
) -> Response:
    p = authorize(request, profile, "read", klass="read")
    want = WantIn(
        decks=_split_csv(decks),
        kinds=_split_csv(kinds) or list(ops.KINDS),
        limit=limit,
        max_new_per_day=max_new_per_day,
        fields=fields,  # type: ignore[arg-type]
        render=render,  # type: ignore[arg-type]
    )
    req = _queue_request(want, cursor)
    result = p.worker.submit("queue", lambda s: ops.get_queue(s, req), priority=PRIORITY_READ)
    return _etag_response(request, result.to_dict())


def _split_csv(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


# ---- exchange ---------------------------------------------------------------------------


@router.post("/p/{profile}/exchange")
def post_exchange(profile: str, body: ExchangeRequest, request: Request) -> dict[str, Any]:
    scope = "review" if body.reviews else "read"
    p = authorize(request, profile, scope, klass="write" if body.reviews else "read")
    review_results = _apply_reviews(request, p, body.reviews) if body.reviews else []
    sync_info: dict[str, Any] | None = None
    # The inline sync is a side effect of the write, like after_write autosync; it does not
    # need the `sync` scope, so a read,review device token gets fresh cards.
    can_sync = (
        body.sync == "auto"
        and body.sync_timeout_seconds > 0
        and p.worker.session.credentials is not None
        and p.worker.cfg.autosync != "off"
    )
    if can_sync:
        try:
            result = p.worker.sync(timeout=body.sync_timeout_seconds)
            sync_info = result.to_dict()
        except ApiError as exc:
            sync_info = {"error": exc.to_dict()["error"]}
    req = _queue_request(body.want, None)
    queue = p.worker.submit("queue", lambda s: ops.get_queue(s, req), priority=PRIORITY_READ)
    return {"reviews": review_results, "sync": sync_info, **queue.to_dict()}


# ---- stats / decks ----------------------------------------------------------------------


@router.get("/p/{profile}/stats")
def get_stats(
    profile: str, request: Request, days: int = Query(default=365, ge=1, le=3650)
) -> Response:
    p = authorize(request, profile, "read", klass="read")
    result = p.worker.submit("stats", lambda s: ops.get_stats(s, days), priority=PRIORITY_READ)
    return _etag_response(request, result)


@router.get("/p/{profile}/decks")
def get_decks(profile: str, request: Request) -> Response:
    p = authorize(request, profile, "read", klass="read")
    result = p.worker.submit("decks", lambda s: ops.get_decks(s), priority=PRIORITY_READ)
    return _etag_response(request, {"decks": result})


@router.get("/p/{profile}/models")
def get_models(profile: str, request: Request) -> Response:
    p = authorize(request, profile, "read", klass="read")
    result = p.worker.submit("models", lambda s: ops.get_models(s), priority=PRIORITY_READ)
    return _etag_response(request, {"models": result})


@router.get("/p/{profile}/notes")
def get_notes(
    profile: str,
    request: Request,
    query: str = Query(min_length=1, max_length=2000),
    limit: int = Query(default=ops.DEFAULT_SEARCH_LIMIT, ge=1, le=ops.MAX_SEARCH_LIMIT),
    offset: int = Query(default=0, ge=0),
    render: str = Query(default="text", pattern="^(text|html)$"),
) -> Response:
    p = authorize(request, profile, "read", klass="read")
    result = p.worker.submit(
        "search_notes",
        lambda s: ops.search_notes(
            s,
            query,
            limit=limit,
            offset=offset,
            render_mode=render,  # type: ignore[arg-type]
        ),
        priority=PRIORITY_READ,
    )
    return _etag_response(request, result)


# ---- media ------------------------------------------------------------------------------


@router.get("/p/{profile}/media/{filename}")
def get_media(profile: str, filename: str, request: Request) -> Response:
    p = authorize(request, profile, "read", klass="read")
    media_dir = Path(str(p.worker.session.path).removesuffix(".anki2") + ".media")
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise NotFound("no such media", code="media_not_found")
    target = (media_dir / filename).resolve()
    if not str(target).startswith(str(media_dir.resolve()) + "/") or not target.is_file():
        raise NotFound("no such media", code="media_not_found")
    # FileResponse handles Range requests and ETag/Last-Modified.
    return FileResponse(str(target), media_type=content_type_for(filename))


# ---- sync -------------------------------------------------------------------------------


@router.post("/p/{profile}/sync")
def post_sync(profile: str, request: Request, body: SyncRequest | None = None) -> dict[str, Any]:
    body = body or SyncRequest()
    sup = supervisor_of(request)
    if body.force_full is not None:
        token = authorize_admin(request, profile)
        if body.confirm != profile:
            raise ApiError(
                "force_full requires confirm=<profile name>", code="confirmation_required"
            )
        worker = sup.worker(profile)
        sup.store.audit(
            token_id=token.id,
            profile=profile,
            action=f"force_full_{body.force_full}",
            outcome="requested",
        )
        try:
            result = worker.sync(force_full=body.force_full)
        except ApiError as exc:
            sup.store.audit(
                token_id=token.id,
                profile=profile,
                action=f"force_full_{body.force_full}",
                outcome=f"error:{exc.code}",
            )
            raise
        sup.store.audit(
            token_id=token.id,
            profile=profile,
            action=f"force_full_{body.force_full}",
            outcome=result.outcome.value,
        )
        return result.to_dict()
    p = authorize(request, profile, "sync", klass="sync")
    return run_sync(sup, p, wait=body.wait, via=None)


def run_sync(sup: Supervisor, p: Principal, *, wait: bool, via: str | None) -> dict[str, Any]:
    """Incremental ``POST sync`` minus HTTP; the MCP ``sync`` tool runs this too."""
    if not wait:
        p.worker.sync_async()
        audit(sup, p, "sync", "queued", via=via)
        return {"outcome": "queued"}
    try:
        result = p.worker.sync()
    except ApiError as exc:
        audit(sup, p, "sync", f"error:{exc.code}", via=via)
        raise
    audit(sup, p, "sync", result.outcome.value, via=via)
    return result.to_dict()


@router.get("/p/{profile}/sync/status")
def get_sync_status(profile: str, request: Request) -> dict[str, Any]:
    p = authorize(request, profile, "read", klass="read")
    return p.worker.submit("sync_status", lambda s: s.sync_status(), priority=PRIORITY_READ)


# ---- admin ------------------------------------------------------------------------------


@router.get("/admin/profiles")
def admin_profiles(request: Request) -> dict[str, Any]:
    token = authorize_admin(request)
    sup = supervisor_of(request)
    statuses = sup.profiles_status()
    if token.profile is not None:
        statuses = [s for s in statuses if s["profile"] == token.profile]
    return {"profiles": statuses}


@router.get("/admin/metrics")
def admin_metrics(request: Request) -> Response:
    authorize_admin(request)
    return PlainTextResponse(
        supervisor_of(request).metrics.render(), media_type="text/plain; version=0.0.4"
    )


@router.get("/admin/audit")
def admin_audit(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    token = authorize_admin(request)
    rows = supervisor_of(request).store.audit_tail(limit, profile=token.profile)
    return {"entries": rows}


@router.post("/p/{profile}/backup")
def post_backup(profile: str, request: Request) -> dict[str, Any]:
    token = authorize_admin(request, profile)
    sup = supervisor_of(request)
    worker = sup.worker(profile)

    def do(s: Session) -> str:
        return str(s.backup("manual"))

    path = worker.submit("backup", do, priority=PRIORITY_SYNC, timeout=300)
    sup.store.audit(token_id=token.id, profile=profile, action="backup", outcome="ok")
    return {"path": path}


def error_response(exc: ApiError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.code == "rate_limited":
        headers["Retry-After"] = str(int(exc.details.get("retry_after_seconds", 1)) + 1)
    if exc.code == "unauthorized":
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=exc.status, content=exc.to_dict(), headers=headers)
