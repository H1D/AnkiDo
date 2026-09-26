# Changelog

All notable changes to Ankido are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). The `/v1` HTTP API and the configuration file are the
public contract; the AnkiConnect shim follows AnkiConnect's own dialect.

## [Unreleased]

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
  `compose.yaml` example; PyPI package `ankido`.
- Documentation: quickstart, API reference, AnkiConnect shim, client guide, deployment,
  schema-upgrade and full-sync policy, migration.

[Unreleased]: https://github.com/H1D/AnkiDo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/H1D/AnkiDo/releases/tag/v0.1.0
