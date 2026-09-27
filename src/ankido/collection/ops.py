# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operations on an open collection. Every function here runs on the profile's worker thread.

This is the single implementation behind both ``/v1`` and the AnkiConnect shim.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from anki.cards import Card, CardId
from anki.collection import Collection
from anki.consts import (
    CARD_TYPE_LRN,
    CARD_TYPE_NEW,
    CARD_TYPE_RELEARNING,
    CARD_TYPE_REV,
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
)
from anki.dbproxy import DBProxy
from anki.decks import DeckId
from anki.decks_pb2 import DeckTreeNode
from anki.errors import InvalidInput, NotFoundError, SearchError
from anki.notes import Note, NoteId
from anki.scheduler.v3 import Scheduler
from anki.scheduler_pb2 import CardAnswer, QueuedCards

from ankido.collection.media import MediaResolver
from ankido.collection.render import html_to_text, normalize_headword, render
from ankido.collection.session import Session
from ankido.errors import ApiError, NotFound
from ankido.store import Store

Kind = Literal["new", "learning", "due"]
KINDS: tuple[Kind, ...] = ("due", "new", "learning")

_QUEUE_KIND: dict[int, Kind] = {
    QueuedCards.NEW: "new",
    QueuedCards.LEARNING: "learning",
    QueuedCards.REVIEW: "due",
}
_RATING = {1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD, 4: CardAnswer.EASY}
_STATE_ATTR = {1: "again", 2: "hard", 3: "good", 4: "easy"}

MAX_REVIEW_AGE_SECONDS = 30 * 86400
MAX_CLOCK_SKEW_SECONDS = 300
DEFAULT_QUEUE_LIMIT = 60
MAX_QUEUE_LIMIT = 1000


def _db(col: Collection) -> DBProxy:
    assert col.db is not None
    return col.db


def _sched(col: Collection) -> Scheduler:
    return cast(Scheduler, col.sched)


# ---- notes ------------------------------------------------------------------------------


@dataclass
class MediaSpec:
    filename: str | None = None
    data: str | None = None
    path: str | None = None
    url: str | None = None
    fields: list[str] = field(default_factory=list)
    kind: Literal["audio", "picture"] = "audio"


@dataclass
class NoteSpec:
    fields: dict[str, str]
    client_id: str | None = None
    tags: list[str] = field(default_factory=list)
    audio: list[MediaSpec] = field(default_factory=list)
    picture: list[MediaSpec] = field(default_factory=list)
    deck: str | None = None  # per-note override
    model: str | None = None


@dataclass
class AddNotesRequest:
    deck: str
    model: str
    notes: list[NoteSpec]
    tags: list[str] = field(default_factory=list)
    dedupe: Literal["skip", "update", "allow"] = "skip"


def add_notes(
    session: Session, store: Store, resolver: MediaResolver, req: AddNotesRequest
) -> list[dict[str, Any]]:
    col = session.require()
    results: list[dict[str, Any]] = []
    client_ids = [n.client_id for n in req.notes if n.client_id]
    journaled = store.journal_get_many(session.name, "note", client_ids)
    for spec in req.notes:
        if spec.client_id and spec.client_id in journaled:
            results.append({**journaled[spec.client_id], "replayed": True})
            continue
        try:
            result = _add_one(col, resolver, req, spec)
        except ApiError as exc:
            result = {"status": "error", "error": exc.to_dict()["error"]}
        if spec.client_id:
            result["client_id"] = spec.client_id
            if result["status"] != "error":
                store.journal_put(session.name, "note", spec.client_id, result)
        results.append(result)
    return results


def _add_one(
    col: Collection, resolver: MediaResolver, req: AddNotesRequest, spec: NoteSpec
) -> dict[str, Any]:
    model_name = spec.model or req.model
    notetype = col.models.by_name(model_name)
    if notetype is None:
        raise NotFound(f"model {model_name!r} not found", code="model_not_found")
    deck_name = spec.deck or req.deck
    did = col.decks.id(deck_name, create=True)
    assert did is not None
    field_names = col.models.field_names(notetype)
    unknown = set(spec.fields) - set(field_names)
    if unknown:
        raise ApiError(
            f"unknown field(s) for model {model_name!r}: {', '.join(sorted(unknown))}",
            code="unknown_field",
            details={"fields": field_names},
        )
    first_field = field_names[0]
    headword = normalize_headword(spec.fields.get(first_field, ""))
    if not headword and not spec.audio and not spec.picture:
        raise ApiError(f"first field {first_field!r} is empty", code="empty_first_field")

    existing: Note | None = None
    if req.dedupe != "allow" and headword:
        existing = _find_duplicate(col, notetype["id"], first_field, headword)
    if existing is not None and req.dedupe == "skip":
        return {
            "status": "skipped_duplicate",
            "note_id": existing.id,
            "card_ids": list(existing.card_ids()),
            "media": [],
        }

    stored: list[str] = []
    fields = dict(spec.fields)
    for media in spec.audio + spec.picture:
        name, data = resolver.resolve(
            filename=media.filename, data=media.data, path=media.path, url=media.url
        )
        actual = col.media.write_data(name, data)
        stored.append(actual)
        tag = f"[sound:{actual}]" if media.kind == "audio" else f'<img src="{actual}">'
        targets = media.fields or [first_field]
        for target in targets:
            if target not in field_names:
                raise ApiError(f"media target field {target!r} not in model", code="unknown_field")
            fields[target] = (fields.get(target, "") + tag).strip()

    tags = list(dict.fromkeys(req.tags + spec.tags))
    if existing is not None:  # dedupe == "update"
        for name, value in fields.items():
            if value:
                existing[name] = value
        for t in tags:
            existing.add_tag(t)
        col.update_note(existing)
        return {
            "status": "updated",
            "note_id": existing.id,
            "card_ids": list(existing.card_ids()),
            "media": stored,
        }

    note = col.new_note(notetype)
    for name, value in fields.items():
        note[name] = value
    note.tags = tags
    col.add_note(note, did)
    return {
        "status": "added",
        "note_id": note.id,
        "card_ids": list(note.card_ids()),
        "media": stored,
    }


_TAG_RE = re.compile(r"<[^>]+>|\[sound:[^\]]*\]|&[#a-z0-9]+;")


def _quick_key(field_html: str) -> str:
    """Cheap approximation of :func:`normalize_headword` for scanning many notes."""
    return _TAG_RE.sub("", field_html).casefold()


def _find_duplicate(col: Collection, mid: int, first_field: str, headword: str) -> Note | None:
    # SQLite LIKE is ASCII-only and the stored field carries markup, so a SQL prefilter cannot
    # be trusted. Scan this model's notes with a cheap key and confirm with the real normalizer.
    compact = "".join(headword.split())
    for nid, flds in _db(col).all("select id, flds from notes where mid = ?", mid):
        first = flds.split("\x1f", 1)[0]
        if "".join(_quick_key(first).split()) != compact:
            continue
        if normalize_headword(first) == headword:
            return col.get_note(NoteId(nid))
    return None


# ---- reviews ----------------------------------------------------------------------------


@dataclass
class ReviewSpec:
    card_id: int
    ease: int
    client_id: str | None = None
    answered_at: float | None = None  # epoch seconds
    elapsed_s: float | None = None  # seconds before the request was sent
    time_ms: int = 0


def answer_reviews(
    session: Session,
    store: Store,
    reviews: list[ReviewSpec],
    *,
    received_at: float | None = None,
) -> list[dict[str, Any]]:
    col = session.require()
    received_at = received_at or time.time()
    client_ids = [r.client_id for r in reviews if r.client_id]
    journaled = store.journal_get_many(session.name, "review", client_ids)

    # Resolve timestamps first so we can apply in chronological order.
    resolved: list[tuple[int, int, ReviewSpec]] = []  # (answered_ms, index, spec)
    results: list[dict[str, Any] | None] = [None] * len(reviews)
    for i, spec in enumerate(reviews):
        if spec.client_id and spec.client_id in journaled:
            results[i] = {**journaled[spec.client_id], "status": "duplicate"}
            continue
        if spec.answered_at is not None:
            ts = float(spec.answered_at)
        elif spec.elapsed_s is not None:
            ts = received_at - float(spec.elapsed_s)
        else:
            ts = received_at
        if ts > received_at + MAX_CLOCK_SKEW_SECONDS:
            results[i] = _reject(spec, "timestamp_in_future")
            continue
        if ts < received_at - MAX_REVIEW_AGE_SECONDS:
            results[i] = _reject(spec, "timestamp_too_old")
            continue
        if spec.ease not in _RATING:
            results[i] = _reject(spec, "invalid_ease")
            continue
        resolved.append((int(ts * 1000), i, spec))
    resolved.sort(key=lambda t: (t[0], t[1]))

    applied_in_batch: set[str] = set()
    for answered_ms, i, spec in resolved:
        if spec.client_id and spec.client_id in applied_in_batch:
            results[i] = _reject(spec, "duplicate_in_batch")
            continue
        result = _answer_one(col, spec, answered_ms)
        if spec.client_id:
            result["client_id"] = spec.client_id
            # Journal what must never be retried: applied grades and stale ones. A missing card
            # may appear after the next sync, so that rejection stays retryable.
            if result["status"] == "applied" or result.get("reason") == "stale":
                store.journal_put(session.name, "review", spec.client_id, result)
                applied_in_batch.add(spec.client_id)
        results[i] = result
    return [r for r in results if r is not None]


def _reject(spec: ReviewSpec, reason: str) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "rejected", "reason": reason, "card_id": spec.card_id}
    if spec.client_id:
        out["client_id"] = spec.client_id
    return out


def _answer_one(col: Collection, spec: ReviewSpec, answered_ms: int) -> dict[str, Any]:
    try:
        card = col.get_card(CardId(spec.card_id))
    except NotFoundError:
        return _reject(spec, "card_not_found")
    latest = _db(col).scalar("select max(id) from revlog where cid = ?", card.id)
    if latest is not None and int(latest) > answered_ms:
        # A newer review exists (another device won); the latest review is the truth.
        return {**_reject(spec, "stale"), "latest_review_at": int(latest) // 1000}
    states = col._backend.get_scheduling_states(card.id)  # pyright: ignore[reportPrivateUsage]
    new_state = getattr(states, _STATE_ATTR[spec.ease])
    answer = CardAnswer(
        card_id=card.id,
        current_state=states.current,
        new_state=new_state,
        rating=_RATING[spec.ease],
        answered_at_millis=answered_ms,
        milliseconds_taken=max(0, int(spec.time_ms)),
    )
    try:
        _sched(col).answer_card(answer)
    except InvalidInput as exc:
        if "top of queue" not in str(exc):
            raise
        # The v3 scheduler only answers the card at the top of its cached queue. Offline
        # replays arrive in any order, so drop the cache (update_card does that) and retry.
        col.update_card(card)
        _sched(col).answer_card(answer)
    card.load()
    return {
        "status": "applied",
        "card_id": card.id,
        "answered_at": answered_ms // 1000,
        **_card_schedule(col, card),
    }


def _card_schedule(col: Collection, card: Card) -> dict[str, Any]:
    return {
        "interval_days": card.ivl,
        "due": _due_epoch(col, card),
        "queue": _queue_name(card.queue),
        "type": _type_name(card.type),
    }


def _due_epoch(col: Collection, card: Card) -> int | None:
    """Absolute due time in epoch seconds (None for new cards, whose ``due`` is a position)."""
    if card.queue == QUEUE_TYPE_NEW or card.type == CARD_TYPE_NEW:
        return None
    if card.queue == QUEUE_TYPE_LRN:
        return int(card.due)
    if card.queue in (QUEUE_TYPE_REV, QUEUE_TYPE_DAY_LEARN_RELEARN):
        next_day_at = col.sched.day_cutoff
        return int(next_day_at + (card.due - col.sched.today - 1) * 86400)
    return None


def _queue_name(q: int) -> str:
    names: dict[int, str] = {
        QUEUE_TYPE_NEW: "new",
        QUEUE_TYPE_LRN: "learning",
        QUEUE_TYPE_REV: "review",
        QUEUE_TYPE_DAY_LEARN_RELEARN: "learning",
        -1: "suspended",
        -2: "buried",
        -3: "buried",
    }
    return names.get(q, str(q))


def _type_name(t: int) -> str:
    names: dict[int, str] = {
        CARD_TYPE_NEW: "new",
        CARD_TYPE_LRN: "learning",
        CARD_TYPE_REV: "review",
        CARD_TYPE_RELEARNING: "relearning",
    }
    return names.get(t, str(t))


# ---- queue ------------------------------------------------------------------------------


@dataclass
class QueueRequest:
    decks: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=lambda: list(KINDS))
    limit: int = DEFAULT_QUEUE_LIMIT
    max_new_per_day: int | None = None
    cursor: str | None = None
    fields: Literal["compact", "full"] = "compact"
    render: Literal["text", "html"] = "text"

    def signature(self) -> str:
        return json.dumps(
            [self.decks, sorted(self.kinds), self.max_new_per_day, self.fields, self.render]
        )


@dataclass
class QueueResult:
    cards: list[dict[str, Any]]
    counts: dict[str, Any]
    next_cursor: str | None
    decks: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cards": self.cards,
            "counts": self.counts,
            "next_cursor": self.next_cursor,
            "decks": self.decks,
        }


def _encode_cursor(offset: int, sig: str) -> str:
    raw = json.dumps({"o": offset, "s": sig}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, sig: str) -> int:
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
        if data["s"] != sig:
            raise ValueError("signature mismatch")
        return int(data["o"])
    except Exception as exc:
        raise ApiError(
            "cursor is invalid for these query parameters", code="invalid_cursor", retryable=True
        ) from exc


def _select_deck(col: Collection, did: DeckId) -> None:
    if col.decks.get_current_id() != did:
        col.decks.select(did)


def _resolve_decks(col: Collection, names: list[str]) -> list[tuple[str, DeckId]]:
    if not names:
        top = [
            d for d in col.decks.all_names_and_ids(skip_empty_default=True) if "::" not in d.name
        ]
        return [(d.name, DeckId(d.id)) for d in top]
    out: list[tuple[str, DeckId]] = []
    for name in names:
        did = col.decks.id_for_name(name)
        if did is None:
            raise NotFound(f"deck {name!r} not found", code="deck_not_found")
        out.append((name, did))
    return out


def get_queue(session: Session, req: QueueRequest) -> QueueResult:
    col = session.require()
    bad = set(req.kinds) - set(KINDS)
    if bad:
        raise ApiError(f"unknown kinds: {', '.join(sorted(bad))}", code="invalid_kinds")
    limit = max(1, min(req.limit, MAX_QUEUE_LIMIT))
    sig = req.signature()
    offset = _decode_cursor(req.cursor, sig) if req.cursor else 0
    wanted = offset + limit + 1  # one extra to know whether a next page exists

    decks = _resolve_decks(col, req.decks)
    original_deck = col.decks.get_current_id()
    entries: list[tuple[int, str, QueuedCards.QueuedCard]] = []  # (deck_rank, deck, qc)
    counts_total = {"new": 0, "learning": 0, "due": 0}
    new_budget = req.max_new_per_day
    for rank, (name, did) in enumerate(decks):
        _select_deck(col, did)
        # fetch_limit of 0 would mean "just counts"; ask for what is still needed.
        need = max(wanted - len(entries), 1)
        queued = _sched(col).get_queued_cards(fetch_limit=need)
        counts_total["new"] += queued.new_count
        counts_total["learning"] += queued.learning_count
        counts_total["due"] += queued.review_count
        for qc in queued.cards:
            kind = _QUEUE_KIND[qc.queue]
            if kind not in req.kinds:
                continue
            if kind == "new" and new_budget is not None:
                if new_budget <= 0:
                    continue
                new_budget -= 1
            entries.append((rank, name, qc))
        if len(entries) >= wanted:
            break

    page = entries[offset : offset + limit]
    has_more = len(entries) > offset + limit
    cards = [_render_queued(col, rank, name, qc, req) for rank, name, qc in page]
    # "Current deck" is a synced setting; leave it as the user's other devices expect it.
    _select_deck(col, DeckId(original_deck))
    next_cursor = _encode_cursor(offset + limit, sig) if has_more else None
    counts = {**counts_total, "returned": len(cards)}
    return QueueResult(
        cards=cards, counts=counts, next_cursor=next_cursor, decks=[d for d, _ in decks]
    )


def _render_queued(
    col: Collection, rank: int, deck: str, qc: QueuedCards.QueuedCard, req: QueueRequest
) -> dict[str, Any]:
    card = col.get_card(CardId(qc.card.id))
    out = card_payload(col, card, fields=req.fields, render_mode=req.render)
    out["deck_rank"] = rank
    out["kind"] = _QUEUE_KIND[qc.queue]
    out["next"] = list(_sched(col).describe_next_states(qc.states))
    return out


def card_payload(
    col: Collection,
    card: Card,
    *,
    fields: Literal["compact", "full"] = "compact",
    render_mode: Literal["text", "html"] = "text",
) -> dict[str, Any]:
    ro = card.render_output(reload=False, browser=False)
    rendered = render(ro.question_text, ro.answer_text, render_mode)
    # Rendering turns [sound:x] into [anki:play:...]; the filenames live in the av tags.
    media = list(rendered.media)
    for tag in (*ro.question_av_tags, *ro.answer_av_tags):
        name = getattr(tag, "filename", None)
        if isinstance(name, str) and name and name not in media:
            media.append(name)
    note = card.note()
    out: dict[str, Any] = {
        "card_id": card.id,
        "note_id": note.id,
        "deck": col.decks.name(card.current_deck_id()),
        "q": rendered.question,
        "a": rendered.answer,
        "media": media,
        **_card_schedule(col, card),
    }
    if fields == "full":
        notetype = note.note_type()
        assert notetype is not None
        out.update(
            {
                "model": notetype["name"],
                "template_ord": card.ord,
                "fields": {name: note[name] for name in col.models.field_names(notetype)},
                "tags": list(note.tags),
                "question_html": ro.question_text,
                "answer_html": ro.answer_text,
                "css": ro.css,
                "reps": card.reps,
                "lapses": card.lapses,
                "factor": card.factor,
                "mod": card.mod,
                "flags": card.flags,
            }
        )
    return out


# ---- stats / decks --------------------------------------------------------------------


def reviewed_by_day(col: Collection, days: int = 365) -> dict[str, int]:
    """Reviews per Anki day (rollover-aware), keyed by the calendar date the day started on."""
    next_day_at = col.sched.day_cutoff
    since_ms = (next_day_at - days * 86400) * 1000
    rows = _db(col).all(
        "select cast((id/1000 - ?) / 86400 as int) as d, count() from revlog"
        " where id > ? group by d",
        next_day_at,
        since_ms,
    )
    out: dict[str, int] = {}
    for d, n in rows:
        day_start = next_day_at + (int(d) - 1) * 86400
        out[time.strftime("%Y-%m-%d", time.localtime(day_start + 1))] = int(n)
    return out


def reviewed_today(col: Collection) -> int:
    start_ms = (col.sched.day_cutoff - 86400) * 1000
    return int(_db(col).scalar("select count() from revlog where id > ?", start_ms) or 0)


def deck_counts(col: Collection) -> list[dict[str, Any]]:
    tree = col.sched.deck_due_tree()
    out: list[dict[str, Any]] = []

    def walk(node: DeckTreeNode, prefix: str) -> None:
        for child in node.children:
            full = f"{prefix}::{child.name}" if prefix else child.name
            out.append(
                {
                    "id": child.deck_id,
                    "name": full,
                    "level": child.level,
                    "new": child.new_count,
                    "learning": child.learn_count,
                    "due": child.review_count,
                    "total": child.total_including_children,
                    "filtered": child.filtered,
                }
            )
            walk(child, full)

    walk(tree, "")
    return out


def get_stats(session: Session, days: int = 365) -> dict[str, Any]:
    col = session.require()
    prefs = col.get_preferences()
    return {
        "reviewed_by_day": reviewed_by_day(col, days),
        "reviewed_today": reviewed_today(col),
        "decks": deck_counts(col),
        "day_rollover_hour": prefs.scheduling.rollover,
        "next_day_at": col.sched.day_cutoff,
        "today": col.sched.today,
        "collection_mod": col.mod,
    }


def get_decks(session: Session) -> list[dict[str, Any]]:
    col = session.require()
    counts = {d["id"]: d for d in deck_counts(col)}
    out: list[dict[str, Any]] = []
    for d in col.decks.all_names_and_ids(skip_empty_default=True):
        row = counts.get(d.id, {})
        out.append(
            {
                "id": d.id,
                "name": d.name,
                "parent": d.name.rsplit("::", 1)[0] if "::" in d.name else None,
                "new": row.get("new", 0),
                "learning": row.get("learning", 0),
                "due": row.get("due", 0),
                "total": row.get("total", 0),
            }
        )
    return out


def get_models(session: Session) -> list[dict[str, Any]]:
    """Note types with their field names (first field = dedupe key) and card template names."""
    col = session.require()
    out: list[dict[str, Any]] = []
    for entry in col.models.all_names_and_ids():
        notetype = col.models.get(entry.id)  # pyright: ignore[reportArgumentType]
        if notetype is None:
            continue
        out.append(
            {
                "id": int(entry.id),
                "name": entry.name,
                "fields": col.models.field_names(notetype),
                "templates": [t["name"] for t in notetype["tmpls"]],
                "cloze": notetype["type"] == 1,
            }
        )
    return out


DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 500


def search_notes(
    session: Session,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    render_mode: Literal["text", "html"] = "text",
) -> dict[str, Any]:
    """Anki search syntax over notes, newest first. Read-only."""
    col = session.require()
    ids = sorted(find_notes(col, query), reverse=True)
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    page = ids[offset : offset + limit]
    notes: list[dict[str, Any]] = []
    for nid in page:
        note = col.get_note(NoteId(nid))
        notetype = note.note_type()
        assert notetype is not None
        card_ids = [int(c) for c in note.card_ids()]
        decks = list(
            dict.fromkeys(
                col.decks.name(col.get_card(CardId(c)).current_deck_id()) for c in card_ids
            )
        )
        fields = {name: note[name] for name in col.models.field_names(notetype)}
        if render_mode == "text":
            fields = {k: html_to_text(v, markup=False).strip() for k, v in fields.items()}
        notes.append(
            {
                "note_id": note.id,
                "model": notetype["name"],
                "decks": decks,
                "fields": fields,
                "tags": list(note.tags),
                "card_ids": card_ids,
                "mod": note.mod,
            }
        )
    end = offset + len(page)
    return {
        "notes": notes,
        "total": len(ids),
        "next_offset": end if end < len(ids) else None,
    }


def profile_status(session: Session) -> dict[str, Any]:
    info: dict[str, Any] = {
        "profile": session.name,
        "open": session.is_open,
        "collection_exists": session.path.is_file(),
        "last_sync_at": session.last_sync_at,
        "last_sync_error": session.last_sync_error,
        "full_sync_pending": session.full_sync_pending,
        "schema_upgraded": session.schema_upgraded,
        "autosync": session.cfg.autosync,
        "sync_configured": session.credentials is not None,
    }
    if session.is_open:
        col = session.require()
        info["schema_version"] = int(_db(col).scalar("select ver from col"))
        info["collection_mod"] = col.mod
        info["notes"] = int(_db(col).scalar("select count() from notes"))
        info["cards"] = int(_db(col).scalar("select count() from cards"))
    return info


# ---- generic helpers used by the AnkiConnect shim ---------------------------------------


def find_cards(col: Collection, query: str) -> list[int]:
    try:
        return [int(c) for c in col.find_cards(query)]
    except SearchError as exc:
        raise ApiError(f"invalid search: {exc}", code="invalid_search") from exc


def find_notes(col: Collection, query: str) -> list[int]:
    try:
        return [int(n) for n in col.find_notes(query)]
    except SearchError as exc:
        raise ApiError(f"invalid search: {exc}", code="invalid_search") from exc


def cards_info(col: Collection, card_ids: list[int]) -> list[dict[str, Any]]:
    """AnkiConnect-shaped ``cardsInfo``. Unknown ids yield ``{}`` like AnkiConnect does."""
    out: list[dict[str, Any]] = []
    for cid in card_ids:
        try:
            card = col.get_card(CardId(cid))
        except NotFoundError:
            out.append({})
            continue
        note = card.note()
        notetype = note.note_type()
        assert notetype is not None
        ro = card.render_output(reload=False, browser=False)
        names = col.models.field_names(notetype)
        states = col._backend.get_scheduling_states(card.id)  # pyright: ignore[reportPrivateUsage]
        out.append(
            {
                "cardId": card.id,
                "fields": {n: {"value": note[n], "order": i} for i, n in enumerate(names)},
                "fieldOrder": card.ord,
                "question": ro.question_text,
                "answer": ro.answer_text,
                "modelName": notetype["name"],
                "ord": card.ord,
                "deckName": col.decks.name(card.current_deck_id()),
                "css": ro.css,
                "factor": card.factor,
                "interval": card.ivl,
                "note": note.id,
                "type": card.type,
                "queue": card.queue,
                "due": card.due,
                "reps": card.reps,
                "lapses": card.lapses,
                "left": card.left,
                "mod": card.mod,
                "nextReviews": list(_sched(col).describe_next_states(states)),
                "flags": card.flags,
            }
        )
    return out


def notes_info(col: Collection, note_ids: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for nid in note_ids:
        try:
            note = col.get_note(NoteId(nid))
        except NotFoundError:
            out.append({})
            continue
        notetype = note.note_type()
        assert notetype is not None
        names = col.models.field_names(notetype)
        out.append(
            {
                "noteId": note.id,
                "modelName": notetype["name"],
                "tags": list(note.tags),
                "fields": {n: {"value": note[n], "order": i} for i, n in enumerate(names)},
                "cards": [int(c) for c in note.card_ids()],
                "mod": note.mod,
            }
        )
    return out


def cards_mod_time(col: Collection, card_ids: list[int]) -> list[dict[str, Any]]:
    if not card_ids:
        return []
    out: list[dict[str, Any]] = []
    for i in range(0, len(card_ids), 500):
        chunk = card_ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for cid, mod in _db(col).all(f"select id, mod from cards where id in ({marks})", *chunk):
            out.append({"cardId": int(cid), "mod": int(mod)})
    return out
