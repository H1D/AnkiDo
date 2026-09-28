# SPDX-License-Identifier: AGPL-3.0-or-later
"""``POST /api/{profile}`` — AnkiConnect v6 compatibility shim.

A thin translation onto the same operations ``/v1`` uses. Not the primary surface: no
idempotency, string errors, token in the body. Exists so Yomitan, asbplayer and existing scripts
work unchanged.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from anki.cards import CardId
from anki.collection import Collection
from anki.errors import NotFoundError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ankido.api import v1
from ankido.api.deps import Principal, bearer_from, supervisor_of
from ankido.collection import ops
from ankido.collection.session import Session
from ankido.errors import ApiError, Unauthorized
from ankido.logging import get_logger
from ankido.store import TokenRecord
from ankido.worker import PRIORITY_READ, PRIORITY_SYNC, PRIORITY_WRITE, ProfileWorker

log = get_logger("ankido.shim")
router = APIRouter()

ANKICONNECT_VERSION = 6

Handler = Callable[[Session, dict[str, Any]], Any]
# action -> (scope, priority, handler)
_ACTIONS: dict[str, tuple[str, int, Handler]] = {}


def action(name: str, scope: str, priority: int = PRIORITY_READ) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _ACTIONS[name] = (scope, priority, fn)
        return fn

    return deco


def _col(s: Session) -> Collection:
    return s.require()


def _ids(params: dict[str, Any], key: str) -> list[int]:
    raw = params.get(key) or []
    if not isinstance(raw, list):
        raise ApiError(f"{key} must be a list", code="invalid_params")
    return [int(x) for x in raw]  # type: ignore[union-attr]


# ---- misc -------------------------------------------------------------------------------


@action("version", "read")
def _version(s: Session, p: dict[str, Any]) -> int:
    return ANKICONNECT_VERSION


@action("requestPermission", "read")
def _request_permission(s: Session, p: dict[str, Any]) -> dict[str, Any]:
    return {"permission": "granted", "requireApiKey": True, "version": ANKICONNECT_VERSION}


@action("apiReflect", "read")
def _api_reflect(s: Session, p: dict[str, Any]) -> dict[str, Any]:
    wanted = p.get("actions")
    names = sorted(_ACTIONS) if not wanted else [a for a in wanted if a in _ACTIONS]
    return {"scopes": ["actions"], "actions": names}


@action("getProfiles", "read")
def _get_profiles(s: Session, p: dict[str, Any]) -> list[str]:
    return [s.name]


@action("getActiveProfile", "read")
def _get_active_profile(s: Session, p: dict[str, Any]) -> str:
    return s.name


@action("loadProfile", "read")
def _load_profile(s: Session, p: dict[str, Any]) -> bool:
    return p.get("name") == s.name


# ---- decks ------------------------------------------------------------------------------


@action("deckNames", "read")
def _deck_names(s: Session, p: dict[str, Any]) -> list[str]:
    return [d.name for d in _col(s).decks.all_names_and_ids(skip_empty_default=True)]


@action("deckNamesAndIds", "read")
def _deck_names_and_ids(s: Session, p: dict[str, Any]) -> dict[str, int]:
    return {d.name: d.id for d in _col(s).decks.all_names_and_ids(skip_empty_default=True)}


@action("createDeck", "add", PRIORITY_WRITE)
def _create_deck(s: Session, p: dict[str, Any]) -> int:
    return int(ops.create_deck(s, str(p["deck"]))["deck_id"])


@action("deleteDecks", "admin", PRIORITY_WRITE)
def _delete_decks(s: Session, p: dict[str, Any]) -> None:
    col = _col(s)
    for name in p.get("decks") or []:
        did = col.decks.id_for_name(str(name))
        if did is not None:
            col.decks.remove([did])


@action("getDeckConfig", "read")
def _get_deck_config(s: Session, p: dict[str, Any]) -> dict[str, Any]:
    col = _col(s)
    did = col.decks.id_for_name(str(p["deck"]))
    if did is None:
        raise ApiError("deck was not found", code="deck_not_found", status=404)
    return dict(col.decks.config_dict_for_deck_id(did))


@action("getDecks", "read")
def _get_decks(s: Session, p: dict[str, Any]) -> dict[str, list[int]]:
    col = _col(s)
    out: dict[str, list[int]] = {}
    for cid in _ids(p, "cards"):
        try:
            card = col.get_card(CardId(cid))
        except NotFoundError:
            continue
        out.setdefault(col.decks.name(card.current_deck_id()), []).append(cid)
    return out


@action("changeDeck", "add", PRIORITY_WRITE)
def _change_deck(s: Session, p: dict[str, Any]) -> None:
    ops.move_cards(s, _ids(p, "cards"), str(p["deck"]))


# ---- models -----------------------------------------------------------------------------


@action("modelNames", "read")
def _model_names(s: Session, p: dict[str, Any]) -> list[str]:
    return _col(s).models.all_names()


@action("modelNamesAndIds", "read")
def _model_names_and_ids(s: Session, p: dict[str, Any]) -> dict[str, int]:
    return {m.name: m.id for m in _col(s).models.all_names_and_ids()}


@action("modelFieldNames", "read")
def _model_field_names(s: Session, p: dict[str, Any]) -> list[str]:
    col = _col(s)
    nt = col.models.by_name(str(p["modelName"]))
    if nt is None:
        raise ApiError("model was not found", code="model_not_found", status=404)
    return col.models.field_names(nt)


@action("modelStyling", "read")
def _model_styling(s: Session, p: dict[str, Any]) -> dict[str, str]:
    nt = _col(s).models.by_name(str(p["modelName"]))
    if nt is None:
        raise ApiError("model was not found", code="model_not_found", status=404)
    return {"css": nt["css"]}


@action("modelTemplates", "read")
def _model_templates(s: Session, p: dict[str, Any]) -> dict[str, dict[str, str]]:
    nt = _col(s).models.by_name(str(p["modelName"]))
    if nt is None:
        raise ApiError("model was not found", code="model_not_found", status=404)
    return {t["name"]: {"Front": t["qfmt"], "Back": t["afmt"]} for t in nt["tmpls"]}


# ---- notes ------------------------------------------------------------------------------


def _note_spec(raw: dict[str, Any]) -> tuple[ops.AddNotesRequest, ops.NoteSpec]:
    options = raw.get("options") or {}
    allow_dup = bool(options.get("allowDuplicate", False))
    audio = [
        ops.MediaSpec(
            filename=m.get("filename"),
            data=m.get("data"),
            path=m.get("path"),
            url=m.get("url"),
            fields=list(m.get("fields") or []),
            kind="audio",
        )
        for m in raw.get("audio") or []
    ]
    picture = [
        ops.MediaSpec(
            filename=m.get("filename"),
            data=m.get("data"),
            path=m.get("path"),
            url=m.get("url"),
            fields=list(m.get("fields") or []),
            kind="picture",
        )
        for m in raw.get("picture") or []
    ]
    spec = ops.NoteSpec(
        fields={str(k): str(v) for k, v in (raw.get("fields") or {}).items()},
        tags=[str(t) for t in raw.get("tags") or []],
        audio=audio,
        picture=picture,
    )
    req = ops.AddNotesRequest(
        deck=str(raw["deckName"]),
        model=str(raw["modelName"]),
        notes=[spec],
        dedupe="allow" if allow_dup else "skip",
    )
    return req, spec


def _add_note_impl(s: Session, sup: Any, raw: dict[str, Any]) -> int | None:
    req, _ = _note_spec(raw)
    result = ops.add_notes(s, sup.store, sup.media, req)[0]
    if result["status"] == "added":
        return int(result["note_id"])
    if result["status"] == "skipped_duplicate":
        raise ApiError("cannot create note because it is a duplicate", code="duplicate")
    if result["status"] == "error":
        raise ApiError(result["error"]["message"], code=result["error"]["code"])
    return int(result["note_id"])


@action("canAddNotes", "read")
def _can_add_notes(s: Session, p: dict[str, Any]) -> list[bool]:
    col = _col(s)
    out: list[bool] = []
    for raw in p.get("notes") or []:
        try:
            req, spec = _note_spec(raw)
            nt = col.models.by_name(req.model)
            if nt is None:
                out.append(False)
                continue
            names = col.models.field_names(nt)
            first = spec.fields.get(names[0], "")
            hw = ops.normalize_headword(first)
            if not hw:
                out.append(False)
                continue
            dup = req.dedupe == "skip" and ops._find_duplicate(  # pyright: ignore[reportPrivateUsage]
                col, nt["id"], names[0], hw
            )
            out.append(not dup)
        except Exception:
            out.append(False)
    return out


@action("findNotes", "read")
def _find_notes(s: Session, p: dict[str, Any]) -> list[int]:
    return ops.find_notes(_col(s), str(p.get("query", "")))


@action("notesInfo", "read")
def _notes_info(s: Session, p: dict[str, Any]) -> list[dict[str, Any]]:
    return ops.notes_info(_col(s), _ids(p, "notes"))


@action("updateNoteFields", "add", PRIORITY_WRITE)
def _update_note_fields(s: Session, p: dict[str, Any]) -> None:
    raw = p["note"]
    fields = {str(k): str(v) for k, v in (raw.get("fields") or {}).items()}
    # AnkiConnect clients expect last-write-wins and may drop media on purpose.
    update = ops.NoteUpdate(note_id=int(raw["id"]), fields=fields, allow_media_loss=True)
    (result,) = ops.update_notes(s, None, [update])
    if result["status"] == "error":
        err = result["error"]
        raise ApiError(err["message"], code=err["code"], details=err.get("details"))


@action("deleteNotes", "delete", PRIORITY_WRITE)
def _delete_notes(s: Session, p: dict[str, Any]) -> None:
    # _run_action goes through v1.delete_notes instead, for its audit detail and backup timeout.
    ops.delete_notes(s, _ids(p, "notes"))


def _retag(s: Session, p: dict[str, Any], *, remove: bool) -> None:
    tags = str(p.get("tags", "")).split()
    updates = [
        ops.NoteUpdate(
            note_id=n, add_tags=[] if remove else tags, remove_tags=tags if remove else []
        )
        for n in _ids(p, "notes")
    ]
    ops.update_notes(s, None, updates)  # unknown notes are ignored, as in AnkiConnect


@action("addTags", "add", PRIORITY_WRITE)
def _add_tags(s: Session, p: dict[str, Any]) -> None:
    _retag(s, p, remove=False)


@action("removeTags", "add", PRIORITY_WRITE)
def _remove_tags(s: Session, p: dict[str, Any]) -> None:
    _retag(s, p, remove=True)


@action("getTags", "read")
def _get_tags(s: Session, p: dict[str, Any]) -> list[str]:
    return ops.list_tags(s)


# ---- cards ------------------------------------------------------------------------------


@action("findCards", "read")
def _find_cards(s: Session, p: dict[str, Any]) -> list[int]:
    return ops.find_cards(_col(s), str(p.get("query", "")))


@action("cardsInfo", "read")
def _cards_info(s: Session, p: dict[str, Any]) -> list[dict[str, Any]]:
    return ops.cards_info(_col(s), _ids(p, "cards"))


@action("cardsModTime", "read")
def _cards_mod_time(s: Session, p: dict[str, Any]) -> list[dict[str, Any]]:
    return ops.cards_mod_time(_col(s), _ids(p, "cards"))


@action("cardsToNotes", "read")
def _cards_to_notes(s: Session, p: dict[str, Any]) -> list[int]:
    col = _col(s)
    out: list[int] = []
    for cid in _ids(p, "cards"):
        try:
            nid = int(col.get_card(CardId(cid)).nid)
        except NotFoundError:
            continue
        if nid not in out:
            out.append(nid)
    return out


@action("suspend", "review", PRIORITY_WRITE)
def _suspend(s: Session, p: dict[str, Any]) -> bool:
    ops.reschedule_cards(s, _ids(p, "cards"), "suspend")
    return True


@action("unsuspend", "review", PRIORITY_WRITE)
def _unsuspend(s: Session, p: dict[str, Any]) -> bool:
    ops.reschedule_cards(s, _ids(p, "cards"), "unsuspend")
    return True


@action("areSuspended", "read")
def _are_suspended(s: Session, p: dict[str, Any]) -> list[bool | None]:
    col = _col(s)
    out: list[bool | None] = []
    for cid in _ids(p, "cards"):
        try:
            out.append(col.get_card(CardId(cid)).queue == -1)
        except NotFoundError:
            out.append(None)
    return out


@action("areDue", "read")
def _are_due(s: Session, p: dict[str, Any]) -> list[bool]:
    col = _col(s)
    due = set(ops.find_cards(col, "is:due"))
    return [cid in due for cid in _ids(p, "cards")]


@action("getIntervals", "read")
def _get_intervals(s: Session, p: dict[str, Any]) -> list[Any]:
    col = _col(s)
    complete = bool(p.get("complete", False))
    out: list[Any] = []
    for cid in _ids(p, "cards"):
        if complete:
            assert col.db is not None
            rows = col.db.list("select ivl from revlog where cid = ? order by id", cid)
            out.append([int(r) for r in rows])
        else:
            try:
                out.append(col.get_card(CardId(cid)).ivl)
            except NotFoundError:
                out.append(0)
    return out


@action("setDueDate", "review", PRIORITY_WRITE)
def _set_due_date(s: Session, p: dict[str, Any]) -> bool:
    ops.reschedule_cards(s, _ids(p, "cards"), "set_due", str(p["days"]))
    return True


@action("forgetCards", "review", PRIORITY_WRITE)
def _forget_cards(s: Session, p: dict[str, Any]) -> None:
    ops.reschedule_cards(s, _ids(p, "cards"), "forget")


@action("getNumCardsReviewedToday", "read")
def _reviewed_today(s: Session, p: dict[str, Any]) -> int:
    return ops.reviewed_today(_col(s))


@action("getNumCardsReviewedByDay", "read")
def _reviewed_by_day(s: Session, p: dict[str, Any]) -> list[list[Any]]:
    by_day = ops.reviewed_by_day(_col(s), days=3650)
    return [[d, n] for d, n in sorted(by_day.items(), reverse=True)]


@action("deckDueTree", "read")
def _deck_due_tree(s: Session, p: dict[str, Any]) -> list[dict[str, Any]]:
    return ops.deck_counts(_col(s))


# ---- media ------------------------------------------------------------------------------


@action("getMediaDirPath", "read")
def _media_dir(s: Session, p: dict[str, Any]) -> str:
    return _col(s).media.dir()


@action("storeMediaFile", "add", PRIORITY_WRITE)
def _store_media(s: Session, sup_params: dict[str, Any]) -> str:
    raise NotImplementedError  # replaced below with access to the resolver


@action("retrieveMediaFile", "read")
def _retrieve_media(s: Session, p: dict[str, Any]) -> str | bool:
    path = Path(_col(s).media.dir()) / Path(str(p["filename"])).name
    if not path.is_file():
        return False
    return base64.b64encode(path.read_bytes()).decode()


@action("deleteMediaFile", "admin", PRIORITY_WRITE)
def _delete_media(s: Session, p: dict[str, Any]) -> None:
    name = Path(str(p["filename"])).name
    _col(s).media.trash_files([name])


@action("getMediaFilesNames", "read")
def _media_names(s: Session, p: dict[str, Any]) -> list[str]:
    import fnmatch

    pattern = str(p.get("pattern", "*"))
    names = [f.name for f in Path(_col(s).media.dir()).iterdir() if f.is_file()]
    return sorted(fnmatch.filter(names, pattern))


# ---- sync -------------------------------------------------------------------------------


@action("sync", "sync", PRIORITY_SYNC)
def _sync(s: Session, p: dict[str, Any]) -> None:
    s.sync()


@action("syncStatus", "read")
def _sync_status(s: Session, p: dict[str, Any]) -> dict[str, Any]:
    return s.sync_status()


# ---- dispatch ---------------------------------------------------------------------------


def _run_action(
    request: Request, worker: ProfileWorker, token: TokenRecord, name: str, params: dict[str, Any]
) -> Any:
    sup = supervisor_of(request)
    if name == "multi":
        results: list[Any] = []
        for sub in params.get("actions") or []:
            try:
                results.append(
                    {
                        "result": _run_action(
                            request, worker, token, str(sub.get("action")), sub.get("params") or {}
                        ),
                        "error": None,
                    }
                )
            except ApiError as exc:
                results.append({"result": None, "error": exc.message})
        return results
    if name not in _ACTIONS:
        raise ApiError(f"unsupported action: {name}", code="unsupported_action", status=400)
    scope, prio, handler = _ACTIONS[name]
    sup.auth.require(token, worker.name, scope)
    klass = "sync" if prio == PRIORITY_SYNC else ("write" if prio == PRIORITY_WRITE else "read")
    sup.limiter.check(token_id=token.id, profile=worker.name, klass=klass)

    if name == "answerCards":
        return _answer_cards(request, worker, token, params)
    if name == "deleteNotes":
        principal = Principal(token=token, worker=worker, profile=worker.name)
        v1.delete_notes(sup, principal, _ids(params, "notes"), via="ankiconnect")
        return None
    if name == "addNote":
        result = worker.submit(
            name, lambda s: _add_note_impl(s, sup, params["note"]), priority=prio
        )
        worker.note_write()
        _audit(sup, token, worker.name, name, 1)
        return result
    if name == "addNotes":

        def add_many(s: Session) -> list[int | None]:
            out: list[int | None] = []
            for raw in params.get("notes") or []:
                try:
                    out.append(_add_note_impl(s, sup, raw))
                except ApiError:
                    out.append(None)
            return out

        result = worker.submit(name, add_many, priority=prio)
        worker.note_write()
        _audit(sup, token, worker.name, name, sum(r is not None for r in result))
        return result
    if name == "storeMediaFile":

        def store_media(s: Session) -> str:
            fname, data = sup.media.resolve(
                filename=params.get("filename"),
                data=params.get("data"),
                path=params.get("path"),
                url=params.get("url"),
            )
            return s.require().media.write_data(fname, data)

        stored = worker.submit(name, store_media, priority=prio)
        _audit(sup, token, worker.name, name, 1)
        return stored

    timeout = 600.0 if prio == PRIORITY_SYNC else None
    result = worker.submit(name, lambda s: handler(s, params), priority=prio, timeout=timeout)
    if prio == PRIORITY_WRITE:
        worker.note_write()
    if prio != PRIORITY_READ:
        _audit(sup, token, worker.name, name, _count_of(params))
    return result


def _audit(sup: Any, token: TokenRecord, profile: str, action: str, count: int) -> None:
    sup.store.audit(token_id=token.id, profile=profile, action=action, outcome="ok", count=count)


def _count_of(params: dict[str, Any]) -> int:
    for key in ("cards", "notes", "decks", "answers"):
        v = params.get(key)
        if isinstance(v, list):
            return len(v)  # type: ignore[arg-type]
    return 0


def _answer_cards(
    request: Request, worker: ProfileWorker, token: TokenRecord, params: dict[str, Any]
) -> list[bool]:
    """AnkiConnect ``answerCards``: ``{"answers":[{"cardId":..,"ease":..}]}`` → list of bools.

    No idempotency here — this is the documented limitation of the shim.
    """
    sup = supervisor_of(request)
    answers = params.get("answers") or []
    specs = [
        ops.ReviewSpec(
            card_id=int(a["cardId"]), ease=int(a["ease"]), time_ms=int(a.get("timeMs", 0))
        )
        for a in answers
    ]
    received = time.time()
    results = worker.submit(
        "answerCards",
        lambda s: ops.answer_reviews(s, sup.store, specs, received_at=received),
        priority=PRIORITY_WRITE,
    )
    applied = [r["status"] == "applied" for r in results]
    if any(applied):
        worker.note_write()
    _audit(sup, token, worker.name, "answerCards", sum(applied))
    return applied


@router.post("/api/{profile}")
async def ankiconnect(profile: str, request: Request) -> JSONResponse:
    sup = supervisor_of(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"result": None, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"result": None, "error": "request must be an object"}, status_code=400)
    name = str(body.get("action") or "")
    params = body.get("params") or {}
    version = int(body.get("version") or ANKICONNECT_VERSION)
    raw_token = bearer_from(request) or (str(body["key"]) if body.get("key") else None)
    try:
        if raw_token is None:
            raise Unauthorized(
                "missing token: pass Authorization: Bearer or the legacy 'key' field"
            )
        token = sup.auth.authenticate(raw_token)
        worker = sup.worker(profile)
        if not bearer_from(request):
            log.info("legacy key auth used", fields={"profile": profile, "token_id": token.id})
        result = await anyio.to_thread.run_sync(_run_action, request, worker, token, name, params)
    except ApiError as exc:
        # AnkiConnect clients expect HTTP 200 with a string error.
        return JSONResponse({"result": None, "error": exc.message})
    if version < 6:
        return JSONResponse(result)
    return JSONResponse({"result": result, "error": None})


def action_names() -> list[str]:
    return sorted({*_ACTIONS, "multi"})


# answerCards/addNote/addNotes are dispatched specially but must be listed and scoped.
_ACTIONS.setdefault("answerCards", ("review", PRIORITY_WRITE, lambda s, p: None))
_ACTIONS.setdefault("addNote", ("add", PRIORITY_WRITE, lambda s, p: None))
_ACTIONS.setdefault("addNotes", ("add", PRIORITY_WRITE, lambda s, p: None))
