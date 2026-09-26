# Writing your own client

This page is for people building a reviewer on a microcontroller, an e-paper device, a phone
script, or an agent. It assumes [api.md](api.md) for the exact shapes and explains how the pieces
are meant to fit together.

## Authentication

Ask the operator for a token created with `ankido token create --profile <name> --scopes
read,review`. Send it on every request:

```
Authorization: Bearer akd_3f9a1c2b_...
```

Store it in whatever your platform has for secrets; there is no refresh flow, the token is valid
until it expires or is revoked. A `401` means the token is gone: stop retrying and surface it to
the user. A `403` means the token exists but lacks a scope; that is a configuration problem, not
a transient one.

A `read,review` token can fetch cards, media, stats and decks, and grade cards. It cannot add,
delete or sync. That is deliberate: a device that is lost or compromised can at most grade cards.

## The exchange loop (battery devices)

The cheapest session is one request: `POST /v1/p/{profile}/exchange`. Send the reviews that
accumulated since the last session and describe what you want back.

```
wake radio
POST /v1/p/alice/exchange
{
  "reviews": [ ...pending, each with a client_id... ],
  "want": {"decks": ["Dutch::Common"], "kinds": ["due", "learning", "new"],
           "limit": 40, "max_new_per_day": 10},
  "sync_timeout_seconds": 15
}
on 2xx:
  for each entry in response.reviews:
    remove the pending review with that client_id     # applied, duplicate and rejected alike
  replace the local card cache with response.cards
  show response.counts
on network error, 429, 5xx:
  keep the pending reviews; sleep; try again next session
radio off
```

Rules that make this safe:

- Give every review a `client_id` that is unique per profile: a device id plus a counter, or
  `<card_id>-<answered_at>`. Keep the id with the review until the server has acknowledged it.
- Replaying is harmless. If the response was lost, send the same reviews again; each one comes
  back as `duplicate` with the original result and the card is not graded twice. The journal
  keeps ids for 90 days.
- Remove rejected reviews too. Every rejection reason is permanent for the same data
  (`stale`, `timestamp_too_old`, `card_not_found`, ...). Log it and move on.
- Set `sync_timeout_seconds` to what your power budget allows. If the sync does not finish in
  time you still get your reviews applied and a queue from local data; `response.sync` tells you
  what happened (`null` skipped, an object on success, `{"error": ...}` on failure).
- The inline sync does not need the `sync` scope: it is a side effect of the write, like
  `after_write` autosync. A `read,review` device token gets a queue that already reflects the
  other devices. It is skipped when the profile's `autosync` is `off` or has no credentials.
- `exchange` needs `review` when `reviews` is non-empty and only `read` otherwise, so a read-only
  display can use it too.

## Offline replay

Reviews carry their own time so that a device can grade cards for days without a network and
upload later.

- `answered_at`: epoch seconds when the answer was given. Use this if the device has a clock.
- `elapsed_s`: seconds between the answer and the moment the request is sent. Use this if the
  device only has an uptime counter. The server computes `answered_at = received_at - elapsed_s`.
- Neither: the review is dated at receipt.

Limits: more than 300 s in the future is rejected (`timestamp_in_future`); more than 30 days in
the past is rejected (`timestamp_too_old`). Keep your clock roughly right and do not sit on
reviews for a month.

What the server does with the timestamp:

- Reviews in a request are applied in chronological order.
- If the card already has a review newer than yours (someone graded it on the phone after you
  answered it on the device), yours is rejected with `stale` and `latest_review_at`. The newest
  review wins; there is no merging of grades.
- The review log entry uses your timestamp, so day buckets, streaks and `reviewed_by_day` are
  correct even for late uploads.
- The next due date is computed when the review is applied, from the card's state at that
  moment, not backdated. A card graded "good" three days ago offline gets an interval computed
  as of today. That is also what Anki's own clients do when they sync late.

## Reading the compact payload

`fields=compact` (the default) exists so that a client needs no HTML parser. Each card has:

| Field | Use |
| --- | --- |
| `card_id` | Send it back in the review. |
| `q`, `a` | Question and answer text, ready to draw. |
| `media` | Filenames to fetch from `GET /v1/p/{profile}/media/{filename}` if you can play or show them. Ignore otherwise. |
| `kind` | `due`, `new` or `learning`; useful for a colour or a badge. |
| `deck`, `deck_rank` | Where the card came from. `deck_rank` is the index into the `decks` you asked for. |
| `next` | Four strings ("10m", "4d", ...) for again, hard, good, easy: what Anki shows on its answer buttons. |
| `interval_days`, `due`, `queue`, `type` | Scheduling state, if you want to display it. |

Text markup in `q` and `a` (`render=text`):

| Markup | Meaning |
| --- | --- |
| `**text**` | Bold (`<b>`, `<strong>`). |
| `_text_` | Italic (`<i>`, `<em>`). |
| `[...]` or `[hint]` | A cloze deletion on the question side, exactly as Anki renders it. |
| `[answer]` | The revealed cloze on the answer side. |
| `[img:name.png]` | An image was here; `name.png` is also in `media`. |
| `\n` | Line break. Block elements (`<div>`, `<p>`, `<li>`, `<br>`, headings, table rows) become line breaks; runs of more than two are collapsed. |

Everything else (`<style>`, `<script>`, `[anki:play:...]`, `[sound:...]`, other tags) is removed
and HTML entities are decoded. On the answer side the copy of the question that Anki places above
`<hr id=answer>` is cut off, so `a` is only the answer.

Do not assume a screen size. Clients range from 170×320 to 800×480; wrap and page the text
yourself. If you need Anki's HTML and CSS, use `render=html` or `fields=full`.

## Pagination

`GET /queue` returns at most `limit` cards (up to 1000) and a `next_cursor`. Pass it back as
`cursor=` with the same `decks`, `kinds`, `max_new_per_day`, `fields` and `render`; a mismatch
gives `400 invalid_cursor`, which is retryable in the sense that fetching the first page again
fixes it. The cursor is an offset into the current queue: after you grade cards, the queue shifts,
so start from the first page again instead of continuing.

`max_new_per_day` caps new cards in one fetch (across its pages). It does not remember what you
fetched earlier today; Anki's per-deck daily limit still applies underneath.

## `ETag` and `If-None-Match`

`queue`, `stats` and `decks` return an `ETag`. On the next fetch send
`If-None-Match: "<etag>"`; if nothing changed you get `304` with no body and can keep what you
have. This saves transfer and parsing on the client; the server still builds the response to
compare it.

## Compression

Send `Accept-Encoding: gzip`. Responses of 1 KiB and more come back gzip-encoded. A queue of 60
compact cards typically shrinks to a quarter.

## Error handling

Every error is `{"error": {"code", "message", "retryable", "details"?}}` with a meaningful HTTP
status (see the table in [api.md](api.md#error-codes)).

- `retryable: true` (`429`, `502 sync_failed`, `503 profile_busy`, ...): wait and try the same
  request again. On `429` honour `Retry-After` (seconds).
- `retryable: false`: do not resend unchanged. `401` means get a new token; `403` a scope
  problem; `400`/`404`/`422` a bug in the request; `409 sync_required_full` or
  `schema_upgrade_required` means the operator has to act (see
  [schema-upgrade.md](schema-upgrade.md)).
- A per-item failure inside `notes` or `reviews` does not fail the request: check each result's
  `status`.
- Timeouts on your side: treat as "response lost" and replay with the same `client_id`s.

## Idempotent note creation

`POST /notes` takes a `client_id` per note. Results other than `error` are journaled for 90 days;
a repeat with the same `client_id` returns the original result with `"replayed": true` and creates
nothing. Use it whenever a note is created by an automated process (a reader, an agent) that may
be re-run. Errors are not journaled, so after fixing the request the same id can be used again.

Deduplication is separate from idempotency: with the default `dedupe: skip`, a note whose first
field normalizes to the same text as an existing note of the same note type is reported as
`skipped_duplicate` with the existing ids. `update` overwrites the existing note's non-empty
fields instead; `allow` creates a second note.

## A minimal client in shell

```sh
ANKIDO=https://anki.example.com; TOKEN=akd_...

# fetch 5 cards
curl -sS -H "Authorization: Bearer $TOKEN" -H "Accept-Encoding: gzip" --compressed \
  "$ANKIDO/v1/p/alice/queue?decks=Dutch::Common&limit=5" | jq '.cards[] | {card_id, q, a}'

# grade one as "good", answered 20 seconds ago
curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST "$ANKIDO/v1/p/alice/reviews" \
  -d '{"reviews":[{"client_id":"shell-1","card_id":1758880123456,"ease":3,"elapsed_s":20}]}'
```
