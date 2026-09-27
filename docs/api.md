# `/v1` API reference

The `/v1` surface is the primary contract. Everything under `/v1/p/{profile}/` operates on one
profile; `/v1/admin/` spans profiles. There is no generated OpenAPI document or Swagger page; this
file is the reference. The same operations are available to LLM agents as MCP tools at
`/mcp/p/{profile}`; see [mcp.md](mcp.md).

All examples assume:

```sh
export ANKIDO=http://127.0.0.1:8765
export TOKEN=akd_...            # from: ankido token create
alias acurl='curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"'
```

## Conventions

- Authentication: `Authorization: Bearer <token>` on every request except `GET /healthz`. A
  missing or invalid token yields `401` with `WWW-Authenticate: Bearer`.
- Bodies and responses are JSON, UTF-8. Responses of 1 KiB or more are gzip-compressed when the
  request carries `Accept-Encoding: gzip`.
- Requests whose `Content-Length` exceeds `server.max_body_bytes` (default 16 MiB) get `413`.
- Every request to a profile is executed on that profile's single worker thread. Reads are
  served before writes, syncs last. A request that waits longer than
  `server.operation_timeout_seconds` (default 30 s) fails with `503 profile_busy`, which is
  retryable.
- Timestamps are Unix epoch seconds unless the name ends in `_ms`. Ids are Anki's integer ids.

### Scopes

| Scope | Grants |
| --- | --- |
| `read` | `GET queue`, `stats`, `decks`, `models`, `notes`, `media/*`, `sync/status`; shim read actions; MCP read tools |
| `add` | `POST notes`; shim actions that create or modify notes, decks, tags, media; MCP `add_notes` |
| `review` | `POST reviews`, `POST exchange`; shim `answerCards`, suspend, due-date changes; MCP `submit_reviews` |
| `sync` | `POST sync` (incremental); shim `sync` (`exchange` syncs inline without it); MCP `sync` |
| `admin` | Everything above on the token's profile, `POST backup`, `force_full` sync, the `/v1/admin/*` endpoints (limited to the token's profile when bound). A token created with `--admin` has no profile binding and covers every profile. |

A token is bound to one profile unless it was created with `--admin`. Using it on another profile
yields `403 forbidden`.

Tokens issued to MCP clients through OAuth (`akda_…`) only work on the MCP endpoint they were
issued for. Here they get `401 unauthorized`.

### Error shape

Every error, on every endpoint under `/v1`, looks like this:

```json
{
  "error": {
    "code": "forbidden",
    "message": "token lacks scope 'add' on profile 'alice'",
    "retryable": false,
    "details": {"required_scope": "add", "profile": "alice"}
  }
}
```

`details` is present only when there is something to add. `retryable: true` means the same
request may succeed later without changes; back off and retry. `retryable: false` means fix the
request, the token, or the configuration.

### Error codes

| `code` | HTTP | `retryable` | When |
| --- | --- | --- | --- |
| `validation_error` | 422 | false | Body or query failed validation. `details.errors` lists `{loc, msg, type}`. |
| `bad_request` | 400 | false | Generic client error. |
| `unauthorized` | 401 | false | Missing, malformed, unknown, expired, or revoked token. |
| `forbidden` | 403 | false | Token lacks the scope, is bound to another profile, or admin is required. |
| `not_found` | 404 | false | Generic. |
| `profile_not_found` | 404 | false | No profile with that name in the config (only reported after authentication). |
| `length_required` | 411 | false | Request body without `Content-Length` (chunked uploads are not accepted). |
| `deck_not_found` | 404 | false | A deck named in `decks=` does not exist. |
| `model_not_found` | 404 | false | The note type named in `model` does not exist. |
| `media_not_found` | 404 | false | No such file in the profile's media directory. |
| `payload_too_large` | 413 | false | `Content-Length` above `server.max_body_bytes`. |
| `media_too_large` | 413 | false | One attachment above `media.max_bytes`. |
| `rate_limited` | 429 | true | Token or profile bucket exhausted. `Retry-After` header; `details.retry_after_seconds`. |
| `sync_required_full` | 409 | false | AnkiWeb demands a full sync. `details.required` is `full` or `full_upload`; `details.server_message`. See [schema-upgrade.md](schema-upgrade.md). |
| `schema_upgrade_required` | 409 | false | Opening the collection would upgrade its schema and `allow_schema_upgrade` is off. `details.current`, `details.required`. |
| `conflict` | 409 | false | Reserved; not raised by 0.1. |
| `confirmation_required` | 400 | false | `force_full` without `confirm` equal to the profile name. |
| `sync_not_configured` | 400 | false | Profile has no AnkiWeb credentials. |
| `sync_auth_failed` | 502 | false | AnkiWeb rejected the username or password. |
| `sync_failed` | 502 | true | Any other sync failure (network, server). |
| `upstream_error` | 502 | true | Generic upstream failure. |
| `media_fetch_failed` | 502 | true | `url` attachment: host returned a non-200 status or the request failed. |
| `invalid_media` | 400 | false | Not exactly one of `data`, `path`, `url`; bad base64; `data` without `filename`; relative or missing `path`; non-http(s) `url`. |
| `media_host_not_allowed` | 403 | false | `url` host is not in `media.url_allowlist`. (An IP literal as host is refused with 400 and the same code.) |
| `media_path_not_allowed` | 403 | false | `path` is outside `media.path_allowlist`. |
| `unknown_field` | 400 | false | A field name that the note type does not have. `details.fields` lists the valid names. |
| `empty_first_field` | 400 | false | The note's first field is empty after normalization and no media was attached. |
| `invalid_cursor` | 400 | true | The cursor does not belong to these query parameters. Fetch the first page again. |
| `invalid_kinds` | 400 | false | `kinds` contains something other than `due`, `new`, `learning`. |
| `invalid_search` | 400 | false | `GET notes`: the `query` is not valid Anki search syntax. |
| `oauth_not_configured` | 404 (401 on `/mcp`) | false | An OAuth endpoint was called but `server.public_url` is not set. `details.fix` lists the steps. See [mcp.md](mcp.md#when-oauth-is-not-set-up). |
| `oauth_public_url_mismatch` | 400 (401 on `/mcp`) | false | `server.public_url` is plain http, or the request arrived for another host. `details.expected`, `details.seen`, `details.fix`. |
| `profile_busy` | 503 | true | Worker queue full (500 pending operations), operation timed out, or the service is shutting down. |
| `profile_unavailable` | 503 | true | The collection file could not be read. |
| `internal_error` | 500 | false | Unexpected exception; the log has the traceback. |

The AnkiConnect shim maps the same codes to its own string-error format; see
[ankiconnect-shim.md](ankiconnect-shim.md). Codes `invalid_params`, `unsupported_action` and
`duplicate` exist only there.

### Rate limits

Three buckets (`read`, `write`, `sync`) with defaults of 600/120/6 per minute and bursts of
120/40/3. Each bucket is checked once for the token and once for the profile. On exhaustion:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3

{"error":{"code":"rate_limited","message":"rate limit exceeded for write operations","retryable":true,"details":{"retry_after_seconds":2.4}}}
```

`Retry-After` is `retry_after_seconds` rounded up, in whole seconds.

---

## `GET /healthz`

Unauthenticated liveness check. Returns `{"status":"ok"}` and nothing else; it does not open any
collection or contact AnkiWeb.

```sh
curl -s $ANKIDO/healthz
```

---

## `POST /v1/p/{profile}/notes`

Scope `add`. Rate bucket `write`. Adds up to 500 notes in one request.

Request:

```json
{
  "deck": "Dutch::Common",
  "model": "Basic (and reversed card)",
  "tags": ["src:reader"],
  "dedupe": "skip",
  "notes": [
    {
      "client_id": "reader-2026-09-26-0001",
      "fields": {"Front": "het huis", "Back": "house"},
      "tags": ["noun"],
      "audio": [{"filename": "huis.mp3", "data": "<base64>", "fields": ["Front"]}],
      "picture": []
    }
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `deck` | yes | Target deck. Created if missing. `::` separates levels. |
| `model` | yes | Note type name, exact. |
| `tags` | no | Tags applied to every note in the request. |
| `dedupe` | no | `skip` (default): an existing note with the same first field is left alone and reported. `update`: its non-empty fields are overwritten and tags added. `allow`: always create. |
| `notes[].fields` | yes | Field name to value. Values are HTML as Anki stores them. Unknown field names are an error. |
| `notes[].client_id` | no | Idempotency key, up to 200 characters, unique per profile. |
| `notes[].tags` | no | Extra tags for this note. |
| `notes[].deck`, `notes[].model` | no | Per-note override of the request-level values. |
| `notes[].audio[]`, `notes[].picture[]` | no | Attachments; see below. |

Attachments have exactly one source: `data` (base64; `filename` required), `path` (absolute, under
`media.path_allowlist`), or `url` (http or https, host in `media.url_allowlist`, redirects not
followed). `filename` is optional for `path` and `url`. Names are sanitized to
`A-Za-z0-9._ -` and at most 120 characters; Anki may rename on collision, and the name actually
stored is returned. The stored file is referenced in the target `fields` (default: the first
field) as `[sound:name]` for audio and `<img src="name">` for pictures, appended to whatever the
field already contains.

Deduplication compares the first field after removing `[sound:...]`, HTML, and markup,
collapsing whitespace, and case-folding. It only considers notes of the same note type.

Response, one result per note, in request order:

```json
{
  "results": [
    {"status": "added", "note_id": 1758880123450, "card_ids": [1758880123456, 1758880123457],
     "media": ["huis.mp3"], "client_id": "reader-2026-09-26-0001"},
    {"status": "skipped_duplicate", "note_id": 1758700000123, "card_ids": [1758700000130],
     "media": [], "client_id": "reader-2026-09-26-0002"},
    {"status": "updated", "note_id": 1758700000123, "card_ids": [1758700000130],
     "media": ["huis.mp3"]},
    {"status": "error", "error": {"code": "unknown_field", "message": "unknown field(s) for model 'Basic': Frnt",
     "retryable": false, "details": {"fields": ["Front", "Back"]}}, "client_id": "reader-2026-09-26-0003"}
  ]
}
```

A per-note failure does not fail the request: the other notes are still added. Results with a
`client_id` and a status other than `error` are journaled for 90 days; re-sending the same
`client_id` returns the stored result with `"replayed": true` and performs nothing.

```sh
acurl -X POST $ANKIDO/v1/p/alice/notes -d '{
  "deck": "Dutch::Common", "model": "Basic",
  "notes": [{"client_id": "n-1", "fields": {"Front": "het huis", "Back": "house"}}]
}'
```

With `autosync: after_write`, an added or updated note arms a debounced sync
(`after_write_debounce_seconds`, default 30 s).

---

## `POST /v1/p/{profile}/reviews`

Scope `review`. Rate bucket `write`. Grades up to 1000 cards.

Request:

```json
{
  "reviews": [
    {"client_id": "dev7f3a-0012", "card_id": 1758880123456, "ease": 3,
     "answered_at": 1758920000, "time_ms": 4200},
    {"client_id": "dev7f3a-0013", "card_id": 1758880123457, "ease": 1,
     "elapsed_s": 90, "time_ms": 8000}
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `card_id` | yes | Card to grade. |
| `ease` | yes | `1` again, `2` hard, `3` good, `4` easy. |
| `client_id` | no | Idempotency key, up to 200 characters. Strongly recommended. |
| `answered_at` | no | When the answer was given, epoch seconds. |
| `elapsed_s` | no | Alternative to `answered_at` for devices without a clock: seconds between the answer and sending the request. The server subtracts it from the time it received the request. |
| `time_ms` | no | How long the card was shown. Feeds Anki's time statistics. Default 0. |

Without `answered_at` or `elapsed_s` the review is timestamped at receipt. Reviews in one request
are applied in chronological order of their resolved timestamps. Each grade is computed by Anki's
scheduler (`answer_card`); Ankido never touches intervals.

Response, one result per review, in request order:

```json
{
  "results": [
    {"status": "applied", "card_id": 1758880123456, "answered_at": 1758920000,
     "interval_days": 4, "due": 1759264800, "queue": "review", "type": "review",
     "client_id": "dev7f3a-0012"},
    {"status": "duplicate", "card_id": 1758880123457, "answered_at": 1758919910,
     "interval_days": 0, "due": 1758920510, "queue": "learning", "type": "relearning",
     "client_id": "dev7f3a-0013"},
    {"status": "rejected", "reason": "stale", "card_id": 1758880123458,
     "latest_review_at": 1758925000, "client_id": "dev7f3a-0014"}
  ]
}
```

| `status` | Meaning |
| --- | --- |
| `applied` | Graded. `interval_days`, `due` (epoch seconds; `null` for new cards), `queue` (`new`, `learning`, `review`, `suspended`, `buried`) and `type` (`new`, `learning`, `review`, `relearning`) describe the card after grading. |
| `duplicate` | This `client_id` was already processed; the stored result is returned and nothing changed. |
| `rejected` | Not applied. `reason` is one of the values below. |

| `reason` | Meaning |
| --- | --- |
| `timestamp_in_future` | Resolved timestamp more than 300 s after receipt. |
| `timestamp_too_old` | Resolved timestamp more than 30 days before receipt. |
| `invalid_ease` | `ease` outside 1 to 4 (only reachable through the shim; `/v1` validates first). |
| `duplicate_in_batch` | The same `client_id` appeared earlier in this request. |
| `card_not_found` | No such card. |
| `stale` | The card already has a review with a later timestamp (typically graded on another device after this answer was given). `latest_review_at` says when. The newest review wins. |

`stale` rejections are journaled under their `client_id` like applied ones, so a retry returns
`duplicate` and the device can drop the entry. `card_not_found` is not journaled: the card may
arrive with the next sync, so a retry is allowed.

```sh
acurl -X POST $ANKIDO/v1/p/alice/reviews -d '{
  "reviews": [{"client_id": "r-1", "card_id": 1758880123456, "ease": 3, "elapsed_s": 5}]
}'
```

---

## `GET /v1/p/{profile}/queue`

Scope `read`. Rate bucket `read`. Returns cards to study.

| Query parameter | Default | Meaning |
| --- | --- | --- |
| `decks` | all top-level decks | Deck names, comma-separated or repeated. Order is priority: cards from the first deck come first. Each named deck includes its subdecks. Without it, every top-level deck in name order. |
| `kinds` | `due,new,learning` | Which queues to include. |
| `limit` | `60` | Cards per page, 1 to 1000. |
| `max_new_per_day` | none | Cap on new cards in this fetch (across its pages). It is a per-request budget, not tracked over the day; Anki's own per-deck daily limit still applies underneath. |
| `cursor` | none | `next_cursor` from a previous page. |
| `fields` | `compact` | `compact` or `full` (see below). |
| `render` | `text` | `text` or `html`. |

```sh
acurl "$ANKIDO/v1/p/alice/queue?decks=Dutch::Common,Japanese&kinds=due,learning&limit=40"
```

Response:

```json
{
  "cards": [
    {
      "card_id": 1758880123456,
      "note_id": 1758880123450,
      "deck": "Dutch::Common",
      "q": "het huis",
      "a": "house",
      "media": ["huis.mp3"],
      "interval_days": 4,
      "due": 1758931200,
      "queue": "review",
      "type": "review",
      "deck_rank": 0,
      "kind": "due",
      "next": ["<10m", "5d", "9d", "16d"]
    }
  ],
  "counts": {"new": 12, "learning": 3, "due": 41, "returned": 1},
  "next_cursor": "eyJvIjo0MCwicyI6IltbXCJEdXRjaDo6Q29tbW9uXCJdXSJ9",
  "decks": ["Dutch::Common", "Japanese"]
}
```

| Field | Meaning |
| --- | --- |
| `q`, `a` | Question and answer, rendered by Anki, then cleaned. In `text` mode: plain text with `**bold**`, `_italic_`, cloze as `[...]` or `[hint]` on the question and `[answer]` on the answer, `[img:name]` for images, `\n` for line breaks. In `html` mode: Anki's HTML with `<style>`, `<script>`, `[anki:play:...]` and `[sound:...]` removed; the answer side has the repeated question above `<hr id=answer>` cut off. |
| `media` | Filenames referenced by the card (sounds, images, audio/video sources). Fetch each from `/media/{filename}`. External `http(s)` and `data:` sources are not listed. |
| `interval_days`, `due`, `queue`, `type` | Scheduling state; same meaning as in the reviews response. |
| `deck_rank` | Index into `decks`: which of your requested decks the card came from. |
| `kind` | `due`, `new` or `learning`. |
| `next` | Anki's description of the next interval for again, hard, good, easy, in that order. Display only. |
| `counts` | Totals for the selected decks as Anki reports them, before `kinds` filtering, plus `returned`. |
| `decks` | The decks actually queried, in priority order. |

`fields=full` adds `model`, `template_ord`, `fields` (name to raw HTML), `tags`,
`question_html`, `answer_html` (untouched), `css`, `reps`, `lapses`, `factor`, `mod`, `flags`.

### Pagination

`next_cursor` is `null` on the last page. The cursor encodes an offset and a signature of
`decks`, `kinds`, `max_new_per_day`, `fields` and `render`; using it with different parameters
yields `400 invalid_cursor` (retryable: start over without a cursor). Grading cards between pages
changes what is at each offset, so after submitting reviews, fetch page one again.

### `ETag` and `304`

`queue`, `stats` and `decks` responses carry `ETag: "<32 hex>"` and
`Cache-Control: private, max-age=0`. Send `If-None-Match: <etag>` to get `304 Not Modified` with
an empty body when the content is unchanged. The server still computes the response to compare
it, so this saves bandwidth and client-side parsing, not server work.

```sh
acurl -i "$ANKIDO/v1/p/alice/queue?limit=20" -H 'If-None-Match: "2f6c1ab0e3d94c7f8a1b5d6e7f8091a2"'
```

---

## `POST /v1/p/{profile}/exchange`

Scope `review` (always, even with no reviews). Rate bucket `write`. One round trip for a
battery-powered client: apply reviews, sync, return the next batch.

```json
{
  "reviews": [
    {"client_id": "dev7f3a-0012", "card_id": 1758880123456, "ease": 3, "elapsed_s": 130}
  ],
  "want": {
    "decks": ["Dutch::Common"],
    "kinds": ["due", "learning", "new"],
    "limit": 40,
    "max_new_per_day": 10,
    "fields": "compact",
    "render": "text"
  },
  "sync": "auto",
  "sync_timeout_seconds": 20
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `reviews` | `[]` | As in `POST reviews`, up to 1000. |
| `want` | all decks, all kinds, 60, compact text | As the `queue` query parameters, minus `cursor`. |
| `sync` | `auto` | `auto`: sync between applying reviews and building the queue. `never`: skip. |
| `sync_timeout_seconds` | `20` | 0 to 120. How long to wait for the sync; `0` disables it. |

The sync step runs only when all of these hold: `sync` is `auto`, the timeout is above 0, the
profile has credentials, and its `autosync` is not `off`. It does not require the `sync` scope:
it is a side effect of the write, like `after_write` autosync, so a `read,review` device token
gets a queue that already reflects other devices. Scope: `review` when `reviews` is non-empty,
`read` otherwise.

Response: the reviews results, the sync result, and the queue result merged at the top level.

```json
{
  "reviews": [
    {"status": "applied", "card_id": 1758880123456, "answered_at": 1758919870,
     "interval_days": 4, "due": 1759264800, "queue": "review", "type": "review",
     "client_id": "dev7f3a-0012"}
  ],
  "sync": {"outcome": "merged", "local_replaced": false, "server_message": "",
           "media": "synced", "duration_ms": 1840},
  "cards": [ ... ],
  "counts": {"new": 12, "learning": 3, "due": 40, "returned": 40},
  "next_cursor": null,
  "decks": ["Dutch::Common"]
}
```

`sync` is `null` when the step was skipped, or `{"error": {...}}` (the usual error object) when
it failed, for example `sync_required_full` or a timeout as `profile_busy`. A failed sync does
not fail the exchange: the reviews are applied and the queue is returned from local data.

---

## `GET /v1/p/{profile}/stats`

Scope `read`. Rate bucket `read`. `ETag` supported.

| Query parameter | Default | Meaning |
| --- | --- | --- |
| `days` | `365` | How far back `reviewed_by_day` goes, 1 to 3650. |

```sh
acurl "$ANKIDO/v1/p/alice/stats?days=90"
```

```json
{
  "reviewed_by_day": {"2026-09-24": 63, "2026-09-25": 41, "2026-09-26": 12},
  "reviewed_today": 12,
  "decks": [
    {"id": 1758000000001, "name": "Dutch", "level": 1, "new": 20, "learning": 3, "due": 41,
     "total": 812, "filtered": false},
    {"id": 1758000000002, "name": "Dutch::Common", "level": 2, "new": 12, "learning": 3, "due": 41,
     "total": 640, "filtered": false}
  ],
  "day_rollover_hour": 4,
  "next_day_at": 1758945600,
  "today": 1231,
  "collection_mod": 1758920001234
}
```

| Field | Meaning |
| --- | --- |
| `reviewed_by_day` | Review count per Anki day, keyed by the calendar date (container local time) on which that day started. Days with zero reviews are absent. Offline reviews are counted on the day they were answered, not the day they were uploaded. |
| `reviewed_today` | Reviews since the last rollover. |
| `decks` | Every deck in tree order with counts; `total` includes subdecks. |
| `day_rollover_hour` | Hour at which Anki starts a new day (a collection preference, often 4). Clients must use this instead of midnight. |
| `next_day_at` | Epoch seconds of the next rollover. |
| `today` | Anki's day number since collection creation. |
| `collection_mod` | Collection modification time in milliseconds; changes whenever anything changes. |

---

## `GET /v1/p/{profile}/decks`

Scope `read`. Rate bucket `read`. `ETag` supported.

```sh
acurl $ANKIDO/v1/p/alice/decks
```

```json
{
  "decks": [
    {"id": 1758000000001, "name": "Dutch", "parent": null, "new": 20, "learning": 3, "due": 41, "total": 812},
    {"id": 1758000000002, "name": "Dutch::Common", "parent": "Dutch", "new": 12, "learning": 3, "due": 41, "total": 640}
  ]
}
```

`parent` is the full name of the parent deck or `null` for a top-level deck. The empty `Default`
deck is omitted.

---

## `GET /v1/p/{profile}/models`

Scope `read`. Rate bucket `read`. `ETag` supported. The note types of the collection, with the
field names in order (the first field is the one `POST notes` deduplicates on) and the card
template names. Use `name` as `model` in `POST notes`.

```sh
acurl $ANKIDO/v1/p/alice/models
```

```json
{
  "models": [
    {"id": 1758000000100, "name": "Basic", "fields": ["Front", "Back"], "templates": ["Card 1"], "cloze": false},
    {"id": 1758000000101, "name": "Cloze", "fields": ["Text", "Back Extra"], "templates": ["Cloze"], "cloze": true}
  ]
}
```

---

## `GET /v1/p/{profile}/notes`

Scope `read`. Rate bucket `read`. `ETag` supported. Search notes with Anki's search syntax,
newest first. Read-only.

| Query | Default | Meaning |
| --- | --- | --- |
| `query` | required | Anki search, e.g. `deck:Dutch huis`, `tag:verbs`, `added:7`, `"front:kat*"`. `deck:*` matches everything. 1 to 2000 characters. |
| `limit` | `50` | 1 to 500. |
| `offset` | `0` | Skip this many matches. |
| `render` | `text` | `text`: field values with markup, media references and sound tags removed. `html`: the stored field values. |

```sh
acurl "$ANKIDO/v1/p/alice/notes?query=tag:verbs&limit=2"
```

```json
{
  "notes": [
    {
      "note_id": 1758000123456,
      "model": "Basic",
      "decks": ["Dutch::Common"],
      "fields": {"Front": "lopen", "Back": "to walk"},
      "tags": ["verbs"],
      "card_ids": [1758000123457],
      "mod": 1790000000
    }
  ],
  "total": 14,
  "next_offset": 2
}
```

`decks` lists the decks of the note's cards. `next_offset` is `null` on the last page. A query
Anki cannot parse yields `400 invalid_search`.

---

## `GET /v1/p/{profile}/media/{filename}`

Scope `read`. Rate bucket `read`. Serves one file from the profile's media directory with a
content type guessed from the extension (`application/octet-stream` if unknown), `Content-Length`,
`Last-Modified`, `ETag`, and support for `Range` requests (`206 Partial Content`). Names with path
separators are refused with `404 media_not_found`.

```sh
acurl -o huis.mp3 $ANKIDO/v1/p/alice/media/huis.mp3
acurl -o part.mp3 -H "Range: bytes=0-65535" $ANKIDO/v1/p/alice/media/huis.mp3
```

The endpoint needs the `Authorization` header, so a bare `<audio src=...>` in a browser will not
work; fetch with the header and use a blob URL, or proxy it.

---

## `POST /v1/p/{profile}/sync`

Incremental sync with AnkiWeb: collection first, then media (when `media_sync` is on). The body is
optional.

| Field | Default | Meaning |
| --- | --- | --- |
| `wait` | `true` | `true`: run now and return the result. `false`: queue it and return immediately. |
| `force_full` | none | `upload` or `download`. Admin only; see below. |
| `confirm` | none | Must equal the profile name when `force_full` is set. |

### Incremental (scope `sync`, rate bucket `sync`)

```sh
acurl -X POST $ANKIDO/v1/p/alice/sync
```

```json
{
  "outcome": "merged",
  "local_replaced": false,
  "server_message": "",
  "media": "synced",
  "duration_ms": 1840
}
```

| Field | Values |
| --- | --- |
| `outcome` | `no_changes`: nothing was exchanged (the collection's sync counter did not move). `merged`: an incremental two-way exchange happened. `downloaded`: the local collection was replaced by AnkiWeb's; automatic only when the local collection had zero notes and zero cards at sync time, otherwise only after `force_full: download`. `uploaded`: only after `force_full: upload`. |
| `local_replaced` | `true` when the local collection was overwritten by a download. |
| `server_message` | Text from AnkiWeb, usually empty. |
| `media` | `synced`; `skipped` (`media_sync: false`); `in_progress` (still running after 180 s); `failed: <reason>`. |
| `duration_ms` | Wall time of the collection sync plus the media wait. |

With `{"wait": false}` the response is `{"outcome": "queued"}`; the result shows up later in
`GET sync/status`. This call waits up to 600 s, not `operation_timeout_seconds`.

When AnkiWeb requires a full sync and there is anything in the local collection, the call fails
with `409 sync_required_full` and nothing is uploaded or downloaded. `details.required` is
`full_upload` when AnkiWeb specifically wants an upload (typical for a brand-new account that no
client has synced yet) or `full` when either direction would satisfy it. `GET sync/status` then
reports `full_sync_pending: true` until an admin resolves it. Ankido never uploads on its own.

### Forced full sync (scope `admin`)

```sh
acurl -X POST $ANKIDO/v1/p/alice/sync \
  -d '{"force_full": "download", "confirm": "alice"}'
```

`upload` replaces the AnkiWeb copy with the local collection; every other device of the account
then has to download. `download` replaces the local collection with AnkiWeb's. Before either, a
backup named `collection-<timestamp>-pre-force-<direction>.anki2` is written (skipped only when
the local collection has zero notes and zero cards) and an audit entry `force_full_<direction>`
with outcome `requested` is recorded, followed by one with the outcome (`uploaded`, `downloaded`,
or `error:<code>`). `confirm` must be the profile name, else `400 confirmation_required`. The
forced sync runs whether or not AnkiWeb demanded one. A full sync in either direction aborts any
media sync in progress, so Ankido starts a fresh media sync afterwards and waits for it as usual.
Read [schema-upgrade.md](schema-upgrade.md) first.

---

## `GET /v1/p/{profile}/sync/status`

Scope `read`. Rate bucket `read`. Reports the last sync and asks AnkiWeb whether changes are
pending (this makes a network request).

```sh
acurl $ANKIDO/v1/p/alice/sync/status
```

```json
{
  "last_sync_at": 1758920002.5,
  "last_result": {"outcome": "merged", "local_replaced": false, "server_message": "",
                  "media": "synced", "duration_ms": 1840},
  "last_error": null,
  "full_sync_pending": false,
  "configured": true,
  "remote": {"required": "normal_sync", "has_changes": true}
}
```

| Field | Meaning |
| --- | --- |
| `last_sync_at`, `last_result`, `last_error` | Since the service started; `null` if none. `last_error` is an exception class name. |
| `full_sync_pending` | AnkiWeb has demanded a full sync that has not been resolved. |
| `configured` | The profile has credentials. When `false`, `remote` is absent. |
| `remote.required` | `no_changes`, `normal_sync`, or `full_sync`. `remote.has_changes` is `true` for the last two. |
| `remote.error` | Present instead of `required` when AnkiWeb could not be reached; holds the error code. |

---

## `POST /v1/p/{profile}/backup`

Scope `admin` (global, or bound to this profile). Closes the collection briefly, copies it with the
WAL checkpointed into `<data_dir>/<profile>/backups/`, reopens, prunes to `backups.keep`, and
writes an audit entry. Waits up to 300 s.

```sh
acurl -X POST $ANKIDO/v1/p/alice/backup
```

```json
{"path": "/data/alice/backups/collection-20260926-101500-manual.anki2"}
```

---

## `GET /v1/admin/profiles`

Scope `admin`. A profile-bound admin token sees only its own profile.

```sh
acurl $ANKIDO/v1/admin/profiles
```

```json
{
  "profiles": [
    {
      "profile": "alice",
      "open": true,
      "collection_exists": true,
      "last_sync_at": 1758920002.5,
      "last_sync_error": null,
      "full_sync_pending": false,
      "schema_upgraded": false,
      "autosync": "after_write",
      "sync_configured": true,
      "schema_version": 18,
      "collection_mod": 1758920001234,
      "notes": 1234,
      "cards": 2468,
      "queue_depth": 0,
      "syncing": false,
      "current_op": null,
      "oauth": "ok"
    },
    {
      "profile": "bob",
      "open": false,
      "collection_exists": true,
      "last_sync_at": null,
      "last_sync_error": null,
      "full_sync_pending": false,
      "schema_upgraded": false,
      "autosync": "nightly",
      "sync_configured": true,
      "queue_depth": 0,
      "syncing": false,
      "current_op": null,
      "oauth": "ok"
    }
  ]
}
```

`schema_version`, `collection_mod`, `notes` and `cards` are present only while the collection is
open. `schema_upgraded` is `true` if this process upgraded the schema on open. `queue_depth` is
the number of operations waiting for the worker; `current_op` names the one running. `oauth` is
the MCP OAuth setup, the same for every profile: `ok`, `not_configured` (no
`server.public_url`) or `insecure` (plain-http `public_url`); see [mcp.md](mcp.md#oauth).

---

## `GET /v1/admin/metrics`

Scope `admin`. Prometheus text format (`text/plain; version=0.0.4`), in-process counters since
start:

```
ankido_http_requests_total{route="/v1/p/{profile}/queue",status="200"} 1532
ankido_http_request_seconds_bucket{route="/v1/p/{profile}/queue",le="0.1"} 1498
ankido_http_request_seconds_sum{route="/v1/p/{profile}/queue"} 61.2
ankido_http_request_seconds_count{route="/v1/p/{profile}/queue"} 1532
ankido_sync_total{outcome="merged",profile="alice"} 41
```

`route` is the route template, not the concrete path, so profile names do not appear as label
values. `sync_total` counts syncs that went through the worker with outcomes `no_changes`,
`merged`, `downloaded`, `uploaded`, or `error`.

---

## `GET /v1/admin/audit`

Scope `admin`. Newest entries first. A profile-bound admin token sees only its profile.

| Query parameter | Default | Meaning |
| --- | --- | --- |
| `limit` | `100` | 1 to 1000. |

```sh
acurl "$ANKIDO/v1/admin/audit?limit=5"
```

```json
{
  "entries": [
    {"id": 918, "ts": 1758920100, "token_id": "3f9a1c2b", "profile": "alice",
     "action": "reviews", "count": 12, "outcome": "ok", "detail": null},
    {"id": 917, "ts": 1758920002, "token_id": "3f9a1c2b", "profile": "alice",
     "action": "sync", "count": 0, "outcome": "merged", "detail": null},
    {"id": 916, "ts": 1758919000, "token_id": "cli", "profile": "alice",
     "action": "backup", "count": 0, "outcome": "ok", "detail": null}
  ]
}
```

Actions recorded: `notes` (count = notes in the request), `reviews` (count = applied),
`answerCards` from the shim, `sync` (`queued`, an outcome, or `error:<code>`),
`force_full_upload` / `force_full_download`, and `backup`. `token_id` is the short id, or `cli`
for command-line operations. No card content and no secrets are stored.
