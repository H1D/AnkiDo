# Changelog

All notable changes to Ankido are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). The `/v1` HTTP API and the configuration file are the
public contract; the AnkiConnect shim follows AnkiConnect's own dialect.

## [Unreleased]

### Added

- "Type in the answer" cards: queue cards whose template has `{{type:Field}}` carry
  `type_answer`, the expected text prepared the way Anki's `compare_answer` prepares it (cloze
  answers for `{{type:cloze:Field}}`), and `type_nc: true` for `{{type:nc:Field}}`. A client can
  check typed answers offline. The `[[type:...]]` marker stays in `q` and `a` to mark where the
  input box and the comparison go; it is removed when there is nothing to type.

## [0.3.0] - 2026-09-28

### Added

- Editing over `/v1` and MCP, with one implementation shared by `/v1`, MCP and the AnkiConnect
  shim:
  - `PATCH /v1/p/{profile}/notes` / MCP `update_notes` (scope `add`): field values, tags,
    new audio or pictures. Optional `expected_mod` refuses the edit with `stale` if the note
    changed since it was read; an edit that would drop `[sound:…]` or `<img>` references is
    refused with `media_would_be_lost` unless `allow_media_loss` is set. Re-attaching the same
    file is skipped, so retries are harmless.
  - `POST /v1/p/{profile}/notes/delete` / MCP `delete_notes` (new scope `delete`). The first
    delete in an hour writes a `pre-delete` backup; the audit entry lists the deleted ids.
  - `POST /v1/p/{profile}/cards/schedule` / MCP `reschedule_cards` (scope `review`): suspend,
    unsuspend, forget, set due date.
  - `POST /v1/p/{profile}/cards/move` / MCP `move_cards` and `POST /v1/p/{profile}/decks` /
    MCP `create_deck` (scope `add`).
  - `GET /v1/p/{profile}/tags` / MCP `list_tags` (scope `read`).
- MCP `search_notes` takes `format: "html"` to return fields as stored.
- Scope `delete`. OAuth clients can be granted it, but the consent page never ticks it in
  advance.

### Changed

- The shim's `deleteNotes` needs `delete` instead of `admin` (admin tokens still work) and takes
  the `pre-delete` backup. `updateNoteFields` reports unknown fields and missing notes as errors.
- The `review_session` prompt lets the agent offer to fix a card or suspend a leech, with the
  user's agreement; it never deletes.

### Removed

- PyPI publishing. It was never set up, so `ankido` was never on PyPI. Install without
  Docker from the Git repository instead (`uv tool install git+https://github.com/H1D/AnkiDo@vX.Y.Z`).

## [0.2.0] - 2026-09-28

### Added

- MCP server at `/mcp/p/{profile}` (Streamable HTTP, stateless; protocol 2026-07-28, with the
  older `initialize` handshake still accepted). Tools `list_decks`, `list_note_types`,
  `search_notes`, `get_stats`, `get_queue`, `sync_status`, `add_notes`, `submit_reviews`,
  `sync`, and a `review_session` prompt. The tool list follows the token's scopes; every call is
  checked, rate-limited and audited (`via=mcp`) like its `/v1` counterpart. `path` media is not
  accepted over MCP.
- OAuth for MCP clients that cannot send a static header (claude.ai / Claude Desktop custom
  connectors): protected-resource and authorization-server metadata, Client ID Metadata
  Documents, Dynamic Client Registration, PKCE, a consent page where the profile owner pastes an
  existing token, audience-bound access tokens (1 h), rotating refresh tokens (90 days, sliding),
  `POST /oauth/revoke`. Grants appear in `ankido token list` as `oauth:<client>` and are revoked
  together with the token that approved them.
- `server.public_url` (needed for OAuth). When it is missing, plain http, or does not match the
  request's host, the MCP and OAuth endpoints answer `oauth_not_configured` or
  `oauth_public_url_mismatch` with the steps to fix it; `GET /v1/admin/profiles` reports
  `oauth`.
- `GET /v1/p/{profile}/models`: note types with field and template names.
- `GET /v1/p/{profile}/notes?query=`: note search in Anki syntax, newest first, text or HTML
  fields, `limit`/`offset`.
- `server.rate_limits.oauth` for the consent, registration and token endpoints (per address).

### Changed

- `server.trusted_proxies` accepts CIDR ranges and is also used for `X-Forwarded-Host` /
  `X-Forwarded-Proto` in the `public_url` check. Ankido interprets forwarded headers itself;
  uvicorn's proxy-header rewriting is no longer enabled.
- New dependency: the official `mcp` SDK (2.2).
- `ankido.db` gains `oauth_*` tables and two `tokens` columns; they are added on first start.

## [0.1.0] - 2026-09-26

First release.

### Added

- `/v1` REST API, one profile per path: `POST notes` (batch add with `dedupe`, media from
  base64, allowlisted path, or allowlisted URL, `client_id` idempotency), `POST reviews` (grades
  through Anki's scheduler, offline replay with `answered_at` or `elapsed_s`, `client_id`
  journal for 90 days, `stale` rejection when a newer review exists), `GET queue` (deck priority,
  `kinds`, `limit`, `cursor`, `max_new_per_day`, compact text or cleaned HTML rendering, media
  list, `ETag`/`304`), `POST exchange` (reviews plus sync plus next batch in one request),
  `GET stats` (`reviewed_by_day`, per-deck counts, `day_rollover_hour`), `GET decks`,
  `GET media/{filename}` with `Range`, `POST sync` and `GET sync/status`, `POST backup`.
- Admin endpoints: `GET /v1/admin/profiles`, `GET /v1/admin/metrics` (Prometheus text
  format), `GET /v1/admin/audit`. Unauthenticated `GET /healthz`.
- AnkiConnect v6 compatibility shim at `POST /api/{profile}` with 51 actions including
  `addNote`, `addNotes`, `answerCards`, `multi`, `deckDueTree`, `getNumCardsReviewedByDay`,
  `setDueDate`, `forgetCards`, `storeMediaFile`, `sync`; token via `Authorization: Bearer` or
  the legacy `key` field.
- Multi-profile service: one worker thread per profile with a priority queue (reads before
  writes, syncs last), lazy open, idle close, graceful shutdown that flushes a pending sync.
- Incremental AnkiWeb sync through the `anki` library (26.9.3); autosync modes `after_write`
  (debounced), `nightly`, `off`; cached sync key with automatic re-login; media sync.
- Full-sync policy: never an implicit upload (`sync_required_full`); automatic download only
  into an empty local collection; admin `force_full` with `confirm`, backup, and audit entries.
- Schema gate: refuse to open a collection the library would upgrade unless
  `allow_schema_upgrade` is set; backup before upgrading.
- Collection backups with retention (`backups.keep`), on demand and before risky operations.
- Tokens with scopes `read`, `add`, `review`, `sync`, `admin`; profile-bound or global admin;
  argon2 hashes only; expiry; revocation; `ankido token create|list|revoke`.
- CLI: `serve`, `token`, `profile list`, `check-config`, `sync` (with `--force-full`), `backup`.
- Security: bind `127.0.0.1` by default; rate limits per token and per profile for read, write,
  and sync; request body limit; operation timeouts; CORS origin allowlist; media URL host and
  path allowlists; credential files must be mode 0600; secrets scrubbed from logs.
- Append-only audit log of writes, syncs, and backups (token id, action, profile, count,
  outcome).
- Structured JSON logs on stdout; in-process metrics.
- YAML configuration with strict validation; credentials from env files or
  `ANKIDO_PROFILE_<NAME>_USERNAME/_PASSWORD`.
- Docker image `ghcr.io/h1d/ankido` for amd64 and arm64, non-root (uid 1000), healthcheck;
  `compose.yaml` example.
- Documentation: quickstart, API reference, AnkiConnect shim, client guide, deployment,
  schema-upgrade and full-sync policy, migration.

[Unreleased]: https://github.com/H1D/AnkiDo/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/H1D/AnkiDo/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/H1D/AnkiDo/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/H1D/AnkiDo/releases/tag/v0.1.0
