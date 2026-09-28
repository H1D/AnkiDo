# SPDX-License-Identifier: AGPL-3.0-or-later
"""collection/ops.py against a real throwaway collection (Session + Store)."""

from __future__ import annotations

import base64
import math
import time
from pathlib import Path
from typing import Any, Literal

import pytest
from anki.cards import CardId

from ankido.collection.media import MediaResolver
from ankido.collection.ops import (
    DELETE_BACKUP_INTERVAL_SECONDS,
    AddNotesRequest,
    MediaSpec,
    NoteSpec,
    NoteUpdate,
    QueueRequest,
    ReviewSpec,
    add_notes,
    answer_reviews,
    cards_info,
    cards_mod_time,
    create_deck,
    delete_notes,
    find_cards,
    find_notes,
    get_decks,
    get_queue,
    get_stats,
    list_tags,
    move_cards,
    notes_info,
    profile_status,
    reschedule_cards,
    reviewed_today,
    update_notes,
)
from ankido.collection.session import Session
from ankido.errors import ApiError
from ankido.store import Store

Dedupe = Literal["skip", "update", "allow"]


def add_basic(
    session: Session,
    store: Store,
    resolver: MediaResolver,
    n: int = 1,
    *,
    deck: str = "Test",
    prefix: str = "word",
    model: str = "Basic",
    client_prefix: str | None = None,
    dedupe: Dedupe = "skip",
) -> list[dict[str, Any]]:
    notes = [
        NoteSpec(
            fields={"Front": f"{prefix} {i}", "Back": f"meaning {i}"},
            client_id=f"{client_prefix}-{i}" if client_prefix else None,
        )
        for i in range(n)
    ]
    return add_notes(
        session,
        store,
        resolver,
        AddNotesRequest(deck=deck, model=model, notes=notes, dedupe=dedupe),
    )


def first_cards(results: list[dict[str, Any]]) -> list[int]:
    return [int(r["card_ids"][0]) for r in results]


def one(
    session: Session,
    store: Store,
    resolver: MediaResolver,
    fields: dict[str, str],
    *,
    dedupe: Dedupe = "skip",
    **spec_kw: Any,
) -> dict[str, Any]:
    req = AddNotesRequest(
        deck="Test", model="Basic", notes=[NoteSpec(fields=fields, **spec_kw)], dedupe=dedupe
    )
    return add_notes(session, store, resolver, req)[0]


def revlog_ids(session: Session, cid: int) -> list[int]:
    col = session.require()
    assert col.db is not None
    return [int(r) for r in col.db.list("select id from revlog where cid = ? order by id", cid)]


# ---- add_notes ----------------------------------------------------------------------------


class TestAddNotes:
    def test_added_with_card_ids(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        res = add_basic(session, store, resolver, 2)
        assert [r["status"] for r in res] == ["added", "added"]
        for r in res:
            assert isinstance(r["note_id"], int) and len(r["card_ids"]) == 1
            assert r["media"] == [] and "client_id" not in r
        assert session.require().note_count() == 2
        assert session.require().decks.id_for_name("Test") is not None

    def test_reversed_model_yields_two_cards(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        res = add_basic(session, store, resolver, 1, model="Basic (and reversed card)")
        assert len(res[0]["card_ids"]) == 2

    def test_skip_duplicate_on_normalized_headword_plain_then_markup(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        first = one(session, store, resolver, {"Front": "HET HUIS", "Back": "house"})
        dup = one(
            session, store, resolver, {"Front": "het <b>huis</b>[sound:x.mp3]", "Back": "home"}
        )
        assert dup["status"] == "skipped_duplicate"
        assert dup["note_id"] == first["note_id"]
        assert dup["card_ids"] == first["card_ids"]
        assert session.require().note_count() == 1

    def test_skip_duplicate_on_normalized_headword_markup_then_plain(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        one(session, store, resolver, {"Front": "het <b>huis</b>[sound:x.mp3]", "Back": "house"})
        dup = one(session, store, resolver, {"Front": "HET HUIS", "Back": "home"})
        assert dup["status"] == "skipped_duplicate"

    def test_dedupe_fallback_scan_runs_only_when_like_had_candidates(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        # LIKE candidate that is not an exact match -> full scan -> nothing -> added
        one(session, store, resolver, {"Front": "het huis extra", "Back": "x"})
        assert (
            one(session, store, resolver, {"Front": "het huis", "Back": "y"})["status"] == "added"
        )
        # Non-ASCII case differs (SQLite LIKE is ASCII-only), but a sibling candidate
        # "école x" makes the fallback scan run, which then finds "ÉCOLE".
        first = one(session, store, resolver, {"Front": "ÉCOLE", "Back": "x"})
        one(session, store, resolver, {"Front": "école x", "Back": "x"})
        dup = one(session, store, resolver, {"Front": "école", "Back": "y"})
        assert dup["status"] == "skipped_duplicate" and dup["note_id"] == first["note_id"]

    def test_skip_duplicate_non_ascii_case_without_sibling_candidate(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        one(session, store, resolver, {"Front": "ÉCOLE", "Back": "x"})
        assert one(session, store, resolver, {"Front": "école", "Back": "y"})["status"] == (
            "skipped_duplicate"
        )

    def test_update_mode_updates_fields_and_adds_tags(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        first = one(session, store, resolver, {"Front": "big dog", "Back": "old"}, tags=["a"])
        upd = one(
            session,
            store,
            resolver,
            {"Front": "Big <i>Dog</i>", "Back": "new"},
            dedupe="update",
            tags=["b"],
        )
        assert upd["status"] == "updated" and upd["note_id"] == first["note_id"]
        col = session.require()
        note = col.get_note(first["note_id"])
        assert note["Back"] == "new"
        assert note["Front"] == "Big <i>Dog</i>"
        assert set(note.tags) == {"a", "b"}
        assert col.note_count() == 1

    def test_update_mode_without_existing_adds(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        res = one(session, store, resolver, {"Front": "fresh", "Back": "x"}, dedupe="update")
        assert res["status"] == "added"

    def test_allow_mode_adds_duplicate(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        one(session, store, resolver, {"Front": "twin", "Back": "x"})
        res = one(session, store, resolver, {"Front": "twin", "Back": "y"}, dedupe="allow")
        assert res["status"] == "added"
        assert session.require().note_count() == 2

    def test_client_id_replay_returns_same_result_with_replayed_flag(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        first = add_basic(session, store, resolver, 2, client_prefix="reader-0001")
        assert [r["client_id"] for r in first] == ["reader-0001-0", "reader-0001-1"]
        again = add_basic(session, store, resolver, 2, client_prefix="reader-0001", dedupe="allow")
        assert all(r["replayed"] is True for r in again)
        assert [r["note_id"] for r in again] == [r["note_id"] for r in first]
        assert [r["status"] for r in again] == ["added", "added"]
        assert session.require().note_count() == 2
        assert store.journal_get("alice", "note", "reader-0001-0") is not None

    def test_error_results_are_not_journaled(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        bad = one(session, store, resolver, {"Nope": "x"}, client_id="c1")
        assert bad["status"] == "error" and bad["client_id"] == "c1"
        assert store.journal_get("alice", "note", "c1") is None
        good = one(session, store, resolver, {"Front": "x", "Back": "y"}, client_id="c1")
        assert good["status"] == "added" and "replayed" not in good

    def test_per_note_errors(self, session: Session, store: Store, resolver: MediaResolver) -> None:
        unknown = one(session, store, resolver, {"Front": "a", "Nope": "b"})
        assert unknown["error"]["code"] == "unknown_field"
        assert unknown["error"]["details"]["fields"] == ["Front", "Back"]
        model = one(session, store, resolver, {"Front": "a"}, model="No Such Model")
        assert model["error"]["code"] == "model_not_found"
        empty = one(session, store, resolver, {"Front": "<b></b>[sound:x.mp3]", "Back": "b"})
        assert empty["error"]["code"] == "empty_first_field"
        target = one(
            session,
            store,
            resolver,
            {"Front": "a"},
            audio=[MediaSpec(filename="t.mp3", data=_b64(b"x"), fields=["Nope"])],
        )
        assert target["error"]["code"] == "unknown_field"
        media = one(
            session,
            store,
            resolver,
            {"Front": "a"},
            audio=[MediaSpec(filename="t.mp3", data="!!!")],
        )
        assert media["error"]["code"] == "invalid_media"
        # A batch keeps going after a bad note and reports per note.
        req = AddNotesRequest(
            deck="Test",
            model="Basic",
            notes=[NoteSpec(fields={"Nope": "x"}), NoteSpec(fields={"Front": "ok"})],
        )
        statuses = [r["status"] for r in add_notes(session, store, resolver, req)]
        assert statuses == ["error", "added"]

    def test_audio_data_stored_and_sound_tag_appended_to_target_field(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        res = one(
            session,
            store,
            resolver,
            {"Front": "het huis", "Back": "house"},
            audio=[MediaSpec(filename="huis.mp3", data=_b64(b"ID3synthetic"), fields=["Back"])],
        )
        assert res["status"] == "added" and res["media"] == ["huis.mp3"]
        col = session.require()
        note = col.get_note(res["note_id"])
        assert note["Front"] == "het huis"
        assert note["Back"] == "house[sound:huis.mp3]"
        assert Path(col.media.dir(), "huis.mp3").read_bytes() == b"ID3synthetic"

    def test_picture_stored_as_img_in_first_field_by_default(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        res = one(
            session,
            store,
            resolver,
            {"Front": "cat", "Back": "kat"},
            picture=[MediaSpec(filename="cat.png", data=_b64(b"\x89PNG"), kind="picture")],
        )
        assert res["media"] == ["cat.png"]
        note = session.require().get_note(res["note_id"])
        assert note["Front"] == 'cat<img src="cat.png">'

    def test_note_with_only_audio_is_allowed(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        res = one(
            session,
            store,
            resolver,
            {"Back": "sound only"},
            audio=[MediaSpec(filename="only.mp3", data=_b64(b"x"))],
        )
        assert res["status"] == "added"
        assert session.require().get_note(res["note_id"])["Front"] == "[sound:only.mp3]"

    def test_per_note_deck_and_model_override_and_tag_merge(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        req = AddNotesRequest(
            deck="Test",
            model="Basic",
            tags=["batch", "shared"],
            notes=[
                NoteSpec(
                    fields={"Text": "{{c1::x}}", "Back Extra": ""},
                    model="Cloze",
                    deck="Other::Sub",
                    tags=["shared", "mine"],
                )
            ],
        )
        res = add_notes(session, store, resolver, req)[0]
        assert res["status"] == "added"
        col = session.require()
        note = col.get_note(res["note_id"])
        assert set(note.tags) == {"batch", "shared", "mine"}  # Anki stores tags sorted
        assert col.decks.name(col.get_card(note.card_ids()[0]).current_deck_id()) == "Other::Sub"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- answer_reviews -----------------------------------------------------------------------


class TestReviews:
    def test_applied_with_schedule(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver, 2))[0]
        before = time.time()
        res = answer_reviews(session, store, [ReviewSpec(card_id=cid, ease=3, time_ms=4200)])[0]
        assert res["status"] == "applied" and res["card_id"] == cid
        assert res["queue"] == "learning" and res["type"] == "learning"
        assert res["interval_days"] == 0
        assert res["due"] is not None and res["due"] > before
        assert abs(res["answered_at"] - before) < 5
        assert "client_id" not in res
        assert reviewed_today(session.require()) == 1

    def test_easy_graduates_to_review_queue(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        res = answer_reviews(session, store, [ReviewSpec(card_id=cid, ease=4)])[0]
        assert res["status"] == "applied" and res["queue"] == "review"
        assert res["interval_days"] >= 1
        assert res["due"] >= session.require().sched.day_cutoff

    def test_idempotency_same_client_id_twice_returns_duplicate_and_interval_unchanged(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        """Spec 8: the dedicated idempotency test — replaying a review must not grade twice."""
        cid = first_cards(add_basic(session, store, resolver))[0]
        spec = ReviewSpec(card_id=cid, ease=4, client_id="dev7f3a-0012", time_ms=1500)
        first = answer_reviews(session, store, [spec])[0]
        assert first["status"] == "applied" and first["client_id"] == "dev7f3a-0012"
        col = session.require()
        card = col.get_card(CardId(cid))
        snapshot = (card.ivl, card.due, card.reps, card.queue, card.type, card.mod)
        assert snapshot[2] == 1
        ids = revlog_ids(session, cid)
        assert len(ids) == 1

        second = answer_reviews(
            session, store, [ReviewSpec(card_id=cid, ease=4, client_id="dev7f3a-0012")]
        )[0]
        assert second["status"] == "duplicate"
        assert second["card_id"] == cid
        assert second["interval_days"] == first["interval_days"]
        assert second["due"] == first["due"]
        assert second["answered_at"] == first["answered_at"]

        card = col.get_card(CardId(cid))
        assert (card.ivl, card.due, card.reps, card.queue, card.type, card.mod) == snapshot
        assert revlog_ids(session, cid) == ids
        assert store.journal_get("alice", "review", "dev7f3a-0012") is not None

    def test_answered_at_two_days_ago_lands_in_that_days_bucket(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        now = time.time()
        ts = now - 2 * 86400
        res = answer_reviews(session, store, [ReviewSpec(card_id=cid, ease=3, answered_at=ts)])[0]
        assert res["status"] == "applied"
        assert res["answered_at"] == int(ts)
        assert revlog_ids(session, cid) == [int(ts * 1000)]

        col = session.require()
        cutoff = col.sched.day_cutoff  # start of the *next* Anki day (rollover-aware)
        # The Anki day containing ts starts at the largest cutoff - k*86400 that is <= ts.
        k = math.ceil((cutoff - ts) / 86400)
        day_start = cutoff - k * 86400
        assert day_start <= ts < day_start + 86400
        expected = time.strftime("%Y-%m-%d", time.localtime(day_start + 1))
        today = time.strftime("%Y-%m-%d", time.localtime(cutoff - 86400 + 1))
        assert expected != today
        stats = get_stats(session)
        assert stats["reviewed_by_day"] == {expected: 1}
        assert stats["reviewed_today"] == 0

    def test_review_now_lands_in_today_bucket(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        answer_reviews(session, store, [ReviewSpec(card_id=cid, ease=3)])
        col = session.require()
        today = time.strftime("%Y-%m-%d", time.localtime(col.sched.day_cutoff - 86400 + 1))
        stats = get_stats(session)
        assert stats["reviewed_by_day"] == {today: 1}
        assert stats["reviewed_today"] == 1

    def test_elapsed_s_variant(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        received = time.time()
        res = answer_reviews(
            session, store, [ReviewSpec(card_id=cid, ease=3, elapsed_s=30)], received_at=received
        )[0]
        assert res["status"] == "applied"
        assert res["answered_at"] == int(received - 30)

    def test_rejections(self, session: Session, store: Store, resolver: MediaResolver) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        now = time.time()
        specs = [
            ReviewSpec(card_id=cid, ease=3, answered_at=now + 1000, client_id="fut"),
            ReviewSpec(card_id=cid, ease=3, answered_at=now - 31 * 86400),
            ReviewSpec(card_id=cid, ease=7),
            ReviewSpec(card_id=cid, ease=0),
            ReviewSpec(card_id=1, ease=3),
            ReviewSpec(card_id=cid, ease=3, answered_at=now + 60),  # within clock skew: ok
        ]
        res = answer_reviews(session, store, specs, received_at=now)
        assert [r["status"] for r in res] == ["rejected"] * 5 + ["applied"]
        assert [r["reason"] for r in res[:5]] == [
            "timestamp_in_future",
            "timestamp_too_old",
            "invalid_ease",
            "invalid_ease",
            "card_not_found",
        ]
        assert res[0]["client_id"] == "fut" and res[0]["card_id"] == cid
        assert store.journal_get("alice", "review", "fut") is None
        assert len(revlog_ids(session, cid)) == 1

    def test_stale_when_newer_review_exists(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        now = time.time()
        applied = answer_reviews(session, store, [ReviewSpec(card_id=cid, ease=3, answered_at=now)])
        assert applied[0]["status"] == "applied"
        res = answer_reviews(
            session, store, [ReviewSpec(card_id=cid, ease=1, answered_at=now - 3600)]
        )[0]
        assert res["status"] == "rejected" and res["reason"] == "stale"
        assert res["latest_review_at"] == int(now)
        assert len(revlog_ids(session, cid)) == 1

    def test_batch_applied_in_chronological_order(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        now = time.time()
        specs = [
            ReviewSpec(card_id=cid, ease=3, client_id="later", answered_at=now),
            ReviewSpec(card_id=cid, ease=1, client_id="earlier", answered_at=now - 100),
        ]
        res = answer_reviews(session, store, specs, received_at=now)
        # results keep request order ...
        assert [r["client_id"] for r in res] == ["later", "earlier"]
        assert [r["status"] for r in res] == ["applied", "applied"]
        # ... but the revlog shows the earlier review was applied first (no stale rejection)
        assert revlog_ids(session, cid) == [int((now - 100) * 1000), int(now * 1000)]

    def test_duplicate_client_id_within_batch(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cid = first_cards(add_basic(session, store, resolver))[0]
        specs = [ReviewSpec(card_id=cid, ease=3, client_id="same")] * 2
        res = answer_reviews(session, store, specs)
        assert res[0]["status"] == "applied"
        assert res[1]["status"] == "rejected" and res[1]["reason"] == "duplicate_in_batch"
        assert len(revlog_ids(session, cid)) == 1

    def test_answering_third_queued_card_first_works(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        add_basic(session, store, resolver, 3)
        queue = get_queue(session, QueueRequest(decks=["Test"]))
        third = queue.cards[2]["card_id"]
        res = answer_reviews(session, store, [ReviewSpec(card_id=third, ease=3)])[0]
        assert res["status"] == "applied" and res["card_id"] == third
        after = get_queue(session, QueueRequest(decks=["Test"], kinds=["new"]))
        assert [c["card_id"] for c in after.cards] == [
            queue.cards[0]["card_id"],
            queue.cards[1]["card_id"],
        ]
        assert after.counts["learning"] == 1


# ---- get_queue ----------------------------------------------------------------------------


class TestQueue:
    def test_compact_payload_shape_and_next_labels(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        add_basic(session, store, resolver, 2, prefix="q")
        result = get_queue(session, QueueRequest())
        assert result.decks == ["Test"]
        assert result.counts == {"new": 2, "learning": 0, "due": 0, "returned": 2}
        assert result.next_cursor is None
        card = result.cards[0]
        assert set(card) == {
            "card_id", "note_id", "deck", "q", "a", "media",
            "interval_days", "due", "queue", "type", "deck_rank", "kind", "next",
        }  # fmt: skip
        assert card["q"] == "q 0" and card["a"] == "meaning 0"
        assert card["deck"] == "Test" and card["deck_rank"] == 0 and card["kind"] == "new"
        assert card["queue"] == "new" and card["type"] == "new" and card["due"] is None
        assert len(card["next"]) == 4 and all(isinstance(x, str) for x in card["next"])
        d = result.to_dict()
        assert set(d) == {"cards", "counts", "next_cursor", "decks"}

    def test_full_fields_and_html_render(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        one(session, store, resolver, {"Front": "<b>bold</b> q", "Back": "a<br>b"}, tags=["t1"])
        text = get_queue(session, QueueRequest(fields="full", render="text")).cards[0]
        assert text["q"] == "**bold** q" and text["a"] == "a\nb"
        for key in (
            "model", "template_ord", "fields", "tags", "question_html", "answer_html",
            "css", "reps", "lapses", "factor", "mod", "flags",
        ):  # fmt: skip
            assert key in text, key
        assert text["model"] == "Basic" and text["tags"] == ["t1"]
        assert text["fields"] == {"Front": "<b>bold</b> q", "Back": "a<br>b"}
        assert "<b>bold</b>" in text["question_html"]
        html = get_queue(session, QueueRequest(render="html")).cards[0]
        assert html["q"] == "<b>bold</b> q" and html["a"] == "a<br>b"

    def test_kinds_filter_and_counts(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cids = first_cards(add_basic(session, store, resolver, 3))
        answer_reviews(session, store, [ReviewSpec(card_id=cids[0], ease=1)])
        learning = get_queue(session, QueueRequest(kinds=["learning"]))
        assert [c["card_id"] for c in learning.cards] == [cids[0]]
        assert learning.cards[0]["kind"] == "learning"
        assert learning.cards[0]["queue"] == "learning" and learning.cards[0]["due"] is not None
        assert learning.counts["learning"] == 1 and learning.counts["new"] == 2
        new = get_queue(session, QueueRequest(kinds=["new"]))
        assert {c["card_id"] for c in new.cards} == set(cids[1:])
        due = get_queue(session, QueueRequest(kinds=["due"]))
        assert due.cards == [] and due.counts["returned"] == 0

    def test_invalid_kinds(self, session: Session) -> None:
        with pytest.raises(ApiError) as ei:
            get_queue(session, QueueRequest(kinds=["new", "bogus"]))
        assert ei.value.code == "invalid_kinds"

    def test_decks_order_sets_deck_rank(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        add_basic(session, store, resolver, 2, deck="A", prefix="a")
        add_basic(session, store, resolver, 2, deck="B", prefix="b")
        result = get_queue(session, QueueRequest(decks=["B", "A"]))
        assert result.decks == ["B", "A"]
        assert [(c["deck"], c["deck_rank"]) for c in result.cards] == [
            ("B", 0), ("B", 0), ("A", 1), ("A", 1),
        ]  # fmt: skip
        assert result.counts["new"] == 4
        # all top-level decks when none given, in Anki's name order
        assert get_queue(session, QueueRequest()).decks == ["A", "B"]

    def test_deck_not_found(self, session: Session) -> None:
        with pytest.raises(ApiError) as ei:
            get_queue(session, QueueRequest(decks=["Nope"]))
        assert ei.value.code == "deck_not_found" and ei.value.status == 404

    def test_pagination_without_overlap_or_gaps(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        cids = set(first_cards(add_basic(session, store, resolver, 5)))
        seen: list[int] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = get_queue(session, QueueRequest(limit=2, cursor=cursor))
            pages += 1
            seen.extend(c["card_id"] for c in page.cards)
            assert page.counts["returned"] == len(page.cards)
            cursor = page.next_cursor
            if cursor is None:
                break
            assert len(page.cards) == 2
        assert pages == 3
        assert len(seen) == 5 and set(seen) == cids

    def test_invalid_cursor_and_cursor_from_other_query(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        add_basic(session, store, resolver, 3)
        with pytest.raises(ApiError) as ei:
            get_queue(session, QueueRequest(limit=1, cursor="garbage!"))
        assert ei.value.code == "invalid_cursor" and ei.value.retryable is True
        cursor = get_queue(session, QueueRequest(limit=1)).next_cursor
        assert cursor is not None
        with pytest.raises(ApiError) as ei:
            get_queue(session, QueueRequest(limit=1, cursor=cursor, render="html"))
        assert ei.value.code == "invalid_cursor"

    def test_max_new_per_day_cap(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        add_basic(session, store, resolver, 5)
        result = get_queue(session, QueueRequest(max_new_per_day=2))
        assert len(result.cards) == 2 and result.counts["returned"] == 2
        assert get_queue(session, QueueRequest(max_new_per_day=0)).cards == []
        assert len(get_queue(session, QueueRequest(limit=100000)).cards) == 5

    def test_media_listed_for_sound_and_img(
        self, session: Session, store: Store, resolver: MediaResolver
    ) -> None:
        one(
            session,
            store,
            resolver,
            {"Front": "het huis", "Back": 'house<img src="huis.png">'},
            audio=[MediaSpec(filename="huis.mp3", data=_b64(b"x"), fields=["Front"])],
        )
        card = get_queue(session, QueueRequest()).cards[0]
        assert card["q"] == "het huis"
        assert set(card["media"]) == {"huis.mp3", "huis.png"}


# ---- stats / decks / status / shim helpers ------------------------------------------------


def test_get_stats_keys(session: Session) -> None:
    stats = get_stats(session, days=30)
    assert set(stats) == {
        "reviewed_by_day", "reviewed_today", "decks", "day_rollover_hour",
        "next_day_at", "today", "collection_mod",
    }  # fmt: skip
    assert stats["reviewed_by_day"] == {} and stats["reviewed_today"] == 0
    assert 0 <= stats["day_rollover_hour"] <= 23
    assert stats["next_day_at"] > time.time()


def test_get_decks_hierarchy_and_counts(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    add_basic(session, store, resolver, 2, deck="Lang::Dutch")
    decks = get_decks(session)
    by_name = {d["name"]: d for d in decks}
    assert set(by_name) == {"Lang", "Lang::Dutch"}
    assert by_name["Lang"]["parent"] is None
    assert by_name["Lang::Dutch"]["parent"] == "Lang"
    assert by_name["Lang::Dutch"]["new"] == 2 and by_name["Lang"]["total"] == 2
    assert set(by_name["Lang"]) == {"id", "name", "parent", "new", "learning", "due", "total"}
    tree = get_stats(session)["decks"]
    assert [(d["name"], d["level"]) for d in tree] == [("Lang", 1), ("Lang::Dutch", 2)]


def test_profile_status_closed_and_open(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    add_basic(session, store, resolver, 1)
    status = profile_status(session)
    assert status["open"] is True and status["profile"] == "alice"
    assert status["schema_version"] == 18 and status["notes"] == 1 and status["cards"] == 1
    assert status["sync_configured"] is False and status["autosync"] == "off"
    session.close()
    closed = profile_status(session)
    assert closed["open"] is False and closed["collection_exists"] is True
    assert "schema_version" not in closed


def test_find_cards_notes_and_invalid_search(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    res = add_basic(session, store, resolver, 2)
    col = session.require()
    assert set(find_cards(col, "deck:Test")) == set(first_cards(res))
    assert set(find_notes(col, "deck:Test")) == {r["note_id"] for r in res}
    assert find_cards(col, "deck:Nope") == []
    for fn in (find_cards, find_notes):
        with pytest.raises(ApiError) as ei:
            fn(col, '"unbalanced')
        assert ei.value.code == "invalid_search"


def test_cards_info_notes_info_and_mod_time_shapes(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    res = one(session, store, resolver, {"Front": "q", "Back": "a"}, tags=["x"])
    cid, nid = res["card_ids"][0], res["note_id"]
    col = session.require()
    info = cards_info(col, [cid, 1])
    assert info[1] == {}
    card = info[0]
    assert set(card) == {
        "cardId", "fields", "fieldOrder", "question", "answer", "modelName", "ord",
        "deckName", "css", "factor", "interval", "note", "type", "queue", "due", "reps",
        "lapses", "left", "mod", "nextReviews", "flags",
    }  # fmt: skip
    assert card["fields"] == {
        "Front": {"value": "q", "order": 0},
        "Back": {"value": "a", "order": 1},
    }
    assert card["deckName"] == "Test" and card["note"] == nid and len(card["nextReviews"]) == 4

    ninfo = notes_info(col, [nid, 1])
    assert ninfo[1] == {}
    assert set(ninfo[0]) == {"noteId", "modelName", "tags", "fields", "cards", "mod"}
    assert ninfo[0]["cards"] == [cid] and ninfo[0]["tags"] == ["x"]

    assert cards_mod_time(col, []) == []
    mods = cards_mod_time(col, [cid, 1])
    assert mods == [{"cardId": cid, "mod": card["mod"]}]


# ---- editing ----------------------------------------------------------------------------


def test_update_notes_fields_tags_and_unchanged(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    [added] = add_basic(session, store, resolver)
    nid = added["note_id"]
    [r] = update_notes(
        session,
        resolver,
        [NoteUpdate(note_id=nid, fields={"Back": "<b>house</b>"}, add_tags=["a b"])],
    )
    assert r["status"] == "updated" and r["tags"] == ["a", "b"]
    note = session.require().get_note(nid)
    assert note["Back"] == "<b>house</b>" and note["Front"] == "word 0"
    [r] = update_notes(session, resolver, [NoteUpdate(note_id=nid, remove_tags=["a"])])
    assert r["tags"] == ["b"]
    # Same values again: nothing to write.
    [r] = update_notes(
        session, resolver, [NoteUpdate(note_id=nid, fields={"Back": "<b>house</b>"})]
    )
    assert r["status"] == "unchanged"


def test_update_notes_per_item_errors(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    [added] = add_basic(session, store, resolver)
    nid = added["note_id"]
    mod = session.require().get_note(nid).mod
    results = update_notes(
        session,
        resolver,
        [
            NoteUpdate(note_id=1, fields={"Back": "x"}),
            NoteUpdate(note_id=nid, fields={"Nope": "x"}),
            NoteUpdate(note_id=nid, fields={"Front": "  "}),
            NoteUpdate(note_id=nid, fields={"Back": "ok"}, expected_mod=mod),
            NoteUpdate(note_id=nid, fields={"Back": "late"}, expected_mod=mod - 1),
        ],
    )
    assert [r["status"] for r in results] == ["error", "error", "error", "updated", "error"]
    codes = [r["error"]["code"] for r in results if r["status"] == "error"]
    assert codes == ["note_not_found", "unknown_field", "empty_first_field", "stale"]
    assert results[1]["error"]["details"]["fields"] == ["Front", "Back"]
    assert session.require().get_note(nid)["Back"] == "ok"


def test_update_notes_guards_media(session: Session, store: Store, resolver: MediaResolver) -> None:
    r = one(
        session,
        store,
        resolver,
        {"Front": "huis", "Back": 'house <img src="h.png">'},
        audio=[MediaSpec(filename="huis.mp3", data=_b64(b"ID3a"))],
    )
    nid = r["note_id"]
    [lost] = update_notes(
        session, resolver, [NoteUpdate(note_id=nid, fields={"Front": "huis", "Back": "house"})]
    )
    assert lost["status"] == "error" and lost["error"]["code"] == "media_would_be_lost"
    assert lost["error"]["details"] == {"field": "Front", "media": ["huis.mp3"]}
    # Keeping the tags is fine, and allow_media_loss drops them on purpose.
    [kept] = update_notes(
        session,
        resolver,
        [NoteUpdate(note_id=nid, fields={"Back": "home <img src='h.png'>"})],
    )
    assert kept["status"] == "updated"
    [dropped] = update_notes(
        session,
        resolver,
        [NoteUpdate(note_id=nid, fields={"Back": "home"}, allow_media_loss=True)],
    )
    assert dropped["status"] == "updated"
    assert session.require().get_note(nid)["Back"] == "home"


def test_update_notes_attaches_media_once(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    [added] = add_basic(session, store, resolver)
    nid = added["note_id"]
    upd = NoteUpdate(
        note_id=nid,
        audio=[MediaSpec(filename="w.mp3", data=_b64(b"ID3w"), fields=["Back"])],
        picture=[MediaSpec(filename="w.png", data=_b64(b"\x89PNG"), kind="picture")],
    )
    [first] = update_notes(session, resolver, [upd])
    assert first["status"] == "updated" and first["media"] == ["w.mp3", "w.png"]
    [retry] = update_notes(session, resolver, [upd])
    assert retry["status"] == "unchanged"
    note = session.require().get_note(nid)
    assert note["Back"] == "meaning 0[sound:w.mp3]"
    assert note["Front"] == 'word 0<img src="w.png">'
    [bad] = update_notes(
        session,
        resolver,
        [NoteUpdate(note_id=nid, audio=[MediaSpec(data=_b64(b"x"), fields=["Nope"])])],
    )
    assert bad["error"]["code"] == "unknown_field"


def test_delete_notes_backs_up_once_an_hour(
    session: Session, store: Store, resolver: MediaResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    added = add_basic(session, store, resolver, 3)
    ids = [r["note_id"] for r in added]
    out = delete_notes(session, [ids[0], 42, ids[0]])
    assert out["results"] == [
        {"note_id": ids[0], "status": "deleted"},
        {"note_id": 42, "status": "not_found"},
    ]
    assert out["backup"] is not None and out["backup"].endswith("-pre-delete.anki2")
    assert (session.backups_dir() / out["backup"]).is_file()
    assert delete_notes(session, [ids[1]])["backup"] is None
    assert delete_notes(session, [42])["backup"] is None  # nothing deleted, nothing backed up
    later = time.time() + DELETE_BACKUP_INTERVAL_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert delete_notes(session, [ids[2]])["backup"] is not None
    assert find_notes(session.require(), "deck:Test") == []


def test_reschedule_cards(session: Session, store: Store, resolver: MediaResolver) -> None:
    cids = first_cards(add_basic(session, store, resolver, 2))
    out = reschedule_cards(session, [cids[0], 7], "suspend")
    assert out[0]["status"] == "ok" and out[0]["queue"] == "suspended"
    assert out[1] == {"card_id": 7, "status": "not_found"}
    assert reschedule_cards(session, [cids[0]], "unsuspend")[0]["queue"] == "new"
    [due] = reschedule_cards(session, [cids[1]], "set_due", "3!")
    assert due["queue"] == "review" and due["interval_days"] == 3 and due["due"] is not None
    [forgot] = reschedule_cards(session, [cids[1]], "forget")
    assert forgot["queue"] == "new" and forgot["due"] is None
    assert revlog_ids(session, cids[1]) != []  # set_due and forget log manual entries, not grades
    for days in (None, "soon", "-1", "3-"):
        with pytest.raises(ApiError) as ei:
            reschedule_cards(session, cids, "set_due", days)
        assert ei.value.code == "invalid_days"
    with pytest.raises(ApiError) as ei:
        reschedule_cards(session, cids, "bury")
    assert ei.value.code == "invalid_action"


def test_move_cards_create_deck_list_tags(
    session: Session, store: Store, resolver: MediaResolver
) -> None:
    added = add_basic(session, store, resolver, 2)
    cids = first_cards(added)
    out = move_cards(session, [cids[0], 9], "Dutch::Verbs")
    assert out["deck"] == "Dutch::Verbs"
    assert [r["status"] for r in out["results"]] == ["moved", "not_found"]
    col = session.require()
    assert col.decks.name(col.get_card(CardId(cids[0])).current_deck_id()) == "Dutch::Verbs"

    made = create_deck(session, "Empty")
    assert made["name"] == "Empty" and made["created"] is True
    again = create_deck(session, "Empty")
    assert again == {**made, "created": False}
    for bad in ("", " :: "):
        with pytest.raises(ApiError) as ei:
            create_deck(session, bad)
        assert ei.value.code == "invalid_deck_name"

    update_notes(
        session, resolver, [NoteUpdate(note_id=added[0]["note_id"], add_tags=["zeta", "Alpha"])]
    )
    assert list_tags(session) == ["Alpha", "zeta"]
