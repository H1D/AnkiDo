# Spec: `Ankido` — a headless service and API for AnkiWeb collections

> Brief for the developer (human or agent). Public open-source project.
> Everything below is a requirement; implementation choices are yours, but deviations from
> "Hard invariants" must be agreed first.

## 1. What we are building

A public repository plus a Docker image. The user puts one or more AnkiWeb accounts in a config file,
runs `docker compose up`, and gets a **lightweight HTTP API to their collections**: add notes (with or
without audio), submit reviews, fetch the queue of due/new cards quickly, read stats. Syncing with
AnkiWeb is **incremental**, over Anki's own protocol. No Anki desktop, no GUI, no X server.

Project name: **Ankido** (暗記 + 道). Package and CLI `ankido`, image `ankido`.

Success = a stranger follows the README, has the service up in 10 minutes, and writes their own client
without reading our source.

## 2. Why this should exist

Today programmatic access to Anki means either **AnkiConnect**, which needs the desktop app running
with a GUI, listens on localhost only, and goes silent while any modal dialog is open; or hand-rolled
scripts on top of the sync protocol, which typically perform a **full-sync upload** and overwrite the
collection on every other device. Neither works on a server, a home device, or inside an agent.

Ankido fills that gap: headless, several accounts in one process, incremental sync, real
authentication, and an API shaped for **battery-powered clients on flaky networks** (MCU/e-paper
reviewers, mobile scripts, LLM agents): offline replay of reviews, idempotency, compact payloads.

## 3. Starting point

Do not start from scratch. Fork `https://github.com/formeo14/anki-connect-server` (Python 3.12+,
FastAPI, built on the official `anki>=26.5` library, single `POST /api` in AnkiConnect v6 format,
incremental sync / `syncStatus` / `syncMedia` done by the library itself, media in `addNote` via
`audio:[{url|data|path}]`). Sibling project worth reading for comparison:
`https://github.com/glechic/anki-connect-server`. Record the base commit and every divergence from
upstream in the fork's README; send generally useful patches upstream where practical.

**Missing in the base, to be written:**
- `answerCards` (only suspend/unsuspend are wired through; grading is absent entirely);
- `getNumCardsReviewedByDay`, `getNumCardsReviewedToday`, `setDueDate`, `forgetCards`;
- queue counts (due/new/learning per deck, `deckDueTree`);
- **any authentication at all** (the base assumes a `127.0.0.1` bind);
- **multi-profile support** (one collection per process, path from a single env var);
- the whole `/v1` layer from section 6.

## 4. Hard invariants

1. **Anki owns scheduling.** Intervals come from `col.sched.answer_card`. Neither the service nor a
   client computes due dates, ease or intervals. FSRS stays Anki's job.
2. **One writer per collection.** An Anki collection is single-writer SQLite: exactly one worker per
   profile and a serialized operation queue; reads must not block behind writes.
3. **No full-sync upload by default.** If the library demands a full sync, the operation is rejected
   with the machine-readable error `sync_required_full`. Forcing it is a separate admin call behind an
   explicit flag, with a collection backup taken first and an audit-log entry.
4. **Schema upgrades require permission.** The `anki` library may be newer than the collection, and
   the first open can bump the schema, which makes every other device of that user demand one full
   sync. Behaviour: refuse by default with a clear message; upgrade only when `allow_schema_upgrade`
   is set for that profile, after a backup. This must be documented in the README — it is the main
   way to ruin someone's data.
5. **No anonymous surface.** No endpoint other than `/healthz` (which carries no data) answers
   without a valid token. Default bind is `127.0.0.1`.
6. **Writes are idempotent.** Re-delivering the same batch of reviews or the same notes must not
   create duplicates or grade a card twice (client-supplied key, see 6.1).
7. **Secrets never reach logs.** No AnkiWeb passwords, no tokens — not in logs, error strings,
   metrics, or the audit log.
8. **No telemetry.** The service talks to nothing except AnkiWeb and the allowlisted hosts used for
   fetching media.

## 5. Architecture

- A supervisor process plus one worker per profile (process or thread with its own loop), behind a
  shared FastAPI app. The profile is always in the path: `/v1/p/{profile}/…`.
- Config: YAML for profiles, env for secrets; secrets may also be supplied as files (docker secrets).
  ```yaml
  profiles:
    alice: { collection: /data/alice/collection.anki2, credentials: /run/secrets/alice.env, autosync: after_write }
    bob:   { collection: /data/bob/collection.anki2,   credentials: /run/secrets/bob.env,   autosync: nightly }
  ```
- Open collections lazily, close them on an idle timeout, and shut down gracefully with a flush and a
  proper close (never leave a collection with a dangling WAL).
- Multi-arch image (amd64 + arm64 — Raspberry Pi is part of the audience), non-root user, healthcheck,
  structured JSON logs, basic metrics.

## 6. API

Two surfaces, one service. **`/v1` is the primary contract** and what clients should target. The
AnkiConnect dialect is a compatibility shim only and gets no new features.

### 6.1 `/v1` — the primary REST API

`Authorization: Bearer <token>`, JSON, meaningful HTTP status codes, one error shape:
`{"error":{"code":"…","message":"…","retryable":bool}}`.

- **`POST /v1/p/{profile}/notes`** — add notes.
  ```json
  { "deck":"Dutch::Common", "model":"Basic (and reversed card)", "tags":["src:reader"],
    "dedupe":"skip|update|allow",
    "notes":[ { "client_id":"reader-2026-09-26-0001",
                "fields":{"Front":"het huis","Back":"house"},
                "audio":[{"filename":"huis.mp3","data":"<base64>","fields":["Front"]}] } ] }
  ```
  - `audio`/`picture` accept `data` (base64), `path`, or `url`; for `url`, host allowlist from the
    config (otherwise the service is an SSRF proxy).
  - Per-note result: `added|updated|skipped_duplicate|error`, `note_id`, stored media filenames.
  - Deduplication keys on the normalized first field (text before `[sound:` and markup), not on exact
    HTML.
- **`POST /v1/p/{profile}/reviews`** — submit grades.
  ```json
  { "reviews":[ {"client_id":"dev7f3a-0012","card_id":1712…,"ease":3,"answered_at":1790…,"time_ms":4200} ] }
  ```
  - Goes through `col.sched.answer_card`. An `answered_at` in the past (offline replay) must not
    corrupt day buckets; document the behaviour and cover it with a test.
  - Idempotent on `client_id` (journal of applied ids, TTL ≥30 days).
  - Per-review result: `applied|duplicate|rejected(reason)`, plus the new `due`/`interval`.
- **`GET /v1/p/{profile}/queue`** — the card queue.
  - Query: `decks=` (ordered list; order is priority), `kinds=due,new,learning`, `limit=`,
    `max_new_per_day=`, `cursor=`, `fields=compact|full`, `render=html|text`.
  - `compact` is the MCU payload: `card_id`, deck priority, and question/answer **ready to draw** —
    the server strips `<style>` and `[anki:play:…]`, expands cloze, and leaves minimal markup; media
    comes as a separate list of references. The goal is that clients carry no HTML-cleanup layer.
  - `ETag`/`cursor` for incremental fetches, `304` when nothing changed, optional gzip.
  - Do not hardcode sizes for a small screen: clients range from 170×320 to 800×480.
- **`POST /v1/p/{profile}/exchange`** — one roundtrip for a battery-powered client:
  `{"reviews":[…], "want":{"decks":[…],"kinds":[…],"limit":60}}` → apply the grades, sync if needed,
  and return the next batch plus counts in the same response. For an MCU that is one radio wake-up
  per session instead of five requests.
- **`GET /v1/p/{profile}/stats`** — `reviewed_by_day` (streaks, heatmaps), due/new/learning counts per
  deck, and `day_rollover_hour` (Anki's day does not start at midnight — clients must be told, not
  left to guess).
- **`GET /v1/p/{profile}/decks`** — deck list with hierarchy and ids.
- **`GET /v1/p/{profile}/media/{filename}`** — serve media with `Range` and a correct content type.
- **`POST /v1/p/{profile}/sync`**, **`GET …/sync/status`** — incremental collection and media sync,
  structured result (`no_changes|merged|downloaded`, whether local data was replaced, server message).
  Autosync modes: `after_write` (debounced), `nightly`, `off`.
- **`GET /healthz`** (unauthenticated, no data) and **`GET /v1/admin/profiles`** (scope `admin`): per
  profile — whether the collection is open, schema version, last sync time, whether a full sync is
  pending, queue depth.

### 6.2 `POST /api/{profile}` — AnkiConnect v6 shim (legacy)

`{"action":…,"version":6,"key":"<token>","params":{…}}` → `{"result":…,"error":null}`. A thin
translation into the same internal operations, not a second engine. It exists so the current ecosystem
(Yomitan, asbplayer, people's own AnkiConnect scripts, AnkiConnect-oriented MCP clients) works without
a rewrite — for outside users that is arguably the headline feature.

The README should state plainly why it is not the primary surface: no idempotency (re-delivering
`answerCards` grades twice), errors as bare strings with no code, token travels in the request body,
no ETag/`304`, no cursor or `Range`, `cardsInfo` returns everything at once, and markup cleanup is
left to the client.

### 6.3 `/mcp/p/{profile}` — MCP for LLM agents (added in 0.2)

- The same operations as `/v1`, exposed as MCP tools: `list_decks`, `list_note_types`,
  `search_notes`, `get_stats`, `get_queue`, `sync_status`, `add_notes`, `submit_reviews`, `sync`,
  plus a `review_session` prompt. Parity rule: every tool maps to a `/v1` endpoint and runs the
  same code; anything new lands in `/v1` first (hence `GET models` and `GET notes?query=`).
- Streamable HTTP, stateless, protocol 2026-07-28, with the legacy `initialize` handshake still
  accepted (statelessly) for older clients. Embedded in the same app, on the profile's worker.
- Auth: static tokens as everywhere else, or OAuth for clients that can only sign in (claude.ai).
  Ankido is its own minimal authorization server: Client ID Metadata Documents and Dynamic
  Client Registration, PKCE S256, a consent page where the owner pastes an existing token, grants
  capped at that token's scopes (never `admin`), audience-bound to one MCP URL, 1 h access and
  90-day rotating refresh tokens, revoked together with the pasted token. OAuth needs
  `server.public_url`; without it the endpoints explain what to configure.
- The tool list is filtered by the caller's scopes and every call is checked again; `path`
  media is not accepted over MCP.

## 7. Security

- **Tokens**: several per profile, scopes `read`, `add`, `review`, `sync`, `admin`. Store only the
  hash (argon2/scrypt) and reveal the secret once at creation:
  `ankido token create --profile alice --scopes read,review`. Support revoke, list, and expiry.
- Tokens go in the `Authorization: Bearer` header; the body field `key` is accepted by the legacy shim
  only.
- **Bind `127.0.0.1` by default.** README: expose it only through a TLS reverse proxy (or an
  identity-aware proxy); never publish the port straight to a LAN or the internet. Optional
  `trusted_proxies` plus `X-Forwarded-For`/`CF-Connecting-IP` trust, for logging only.
- Rate limits per token and per profile (stricter on writes than reads), body size limits, operation
  timeouts.
- CORS off by default, origin allowlist in the config (for browser clients such as web readers).
- Append-only audit log: token id, action, profile, volume, outcome. No secrets, no card content.
- AnkiWeb credentials come from env or 0600 files only; never baked into the image, never logged.
- Recommended device defaults: scopes `read,review`, so a gadget can neither add nor delete cards.
- Back up the collection before a schema upgrade and before any forced full sync, with retention.

## 8. Non-functional requirements

- Latency (collection already open, local call): `notes` with one audio-bearing card ≤300 ms;
  `queue` of 60 cards ≤500 ms; `reviews` batch of 20 ≤300 ms. Sync runs asynchronously, off the
  critical path.
- ≤400 MB RAM per open profile; collections close after idle.
- Tests: unit and integration against a throwaway collection; an end-to-end path "add → sync → appears
  in queue → grade → grade lands on AnkiWeb" against a test account; a dedicated idempotency test
  (same `client_id` twice → `duplicate`, interval unchanged); a regression test for the legacy shim.
  Fixtures must carry real Anki HTML shapes (`<style>` blocks, `[anki:play:…]`, cloze spans,
  `<hr id=answer>`) but be **synthetic** — no one's personal cards in the repo. ≥80% coverage on new
  code.
- CI: ruff, pyright strict, tests, multi-arch image built and published on tag.
- Semver, CHANGELOG, release tags, immutable image tags (not just `latest`).
- Docs in the repo: quickstart (compose + profiles + creating a token), API reference with `curl`
  examples, "writing your own client" (including offline replay and `exchange`), the schema-upgrade
  risk and backups, deploying behind a reverse proxy, and migrating from AnkiConnect or from
  hand-rolled full-sync scripts.
- Pick a license (see 10), add `CONTRIBUTING.md` and issue templates.

## 9. Out of scope

A GUI or web interface (one exception since 0.2: the plain-HTML OAuth consent page for MCP
clients, §6.3); a custom scheduler or a reimplementation of FSRS; scraping AnkiWeb's private
web endpoints (verified not viable); generating translations, TTS, or sourcing audio (callers do that);
mobile apps; running this as a hosted service.

## 10. Decide before starting

1. License: MIT (widest reach) or AGPL (protects against closed SaaS wrappers). Note that the `anki`
   library itself is AGPL, so the choice is largely made for us — confirm the legal reading and state
   it in the README.
2. Does the MCP mode ship in v1 (the base project has one) or slip to v1.1? *Decided: it ships
   in 0.2.0, always on, as described in §6.3.*
3. Claim the name on PyPI, Docker Hub, and GHCR before the first release.
