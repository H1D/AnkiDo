<p align="center">
  <img src="assets/logo.svg" width="160" alt="AnkiDo logo">
</p>

<h1 align="center">AnkiDo</h1>
<p align="center"><em>way of cards</em></p>

Ankido is a headless HTTP API over AnkiWeb collections. You list one or more AnkiWeb accounts in
a YAML file, run one container, and get a token-protected REST API per account: add notes (with
audio or pictures), submit reviews, fetch the due queue as text that is ready to draw, read stats.
Syncing with AnkiWeb is incremental, over Anki's own protocol, through the official `anki`
library. There is no desktop app, no GUI and no display server involved. Anki still owns
scheduling: every grade goes through `col.sched.answer_card`, so FSRS and your deck options apply
exactly as they do on your phone.

## Why

Programmatic access to Anki today means one of two things. AnkiConnect needs the desktop app
running with a window open, listens on localhost only, and stops answering while any dialog is
open. Hand-rolled scripts on top of the sync protocol usually perform a full upload, which
overwrites the collection on every other device and forces each of them through a one-off full
sync. Neither works on a server, on a device in the kitchen, or inside an agent.

Ankido is the third option: headless, several accounts in one process, incremental sync, real
authentication, and an API shaped for battery-powered clients on unreliable networks (e-paper
reviewers, microcontrollers, mobile scripts, LLM agents): offline replay of reviews, idempotent
writes, compact payloads.

## Features

- `POST /notes`: add notes with `audio` or `picture` from base64, an allowlisted path, or an
  allowlisted URL; deduplication on the normalized first field; `client_id` idempotency.
- `POST /reviews`: grade cards through Anki's scheduler; offline replay with `answered_at` or
  `elapsed_s`; duplicates detected by `client_id` for 90 days; stale grades (a newer review from
  another device) rejected instead of applied.
- `GET /queue`: due, new and learning cards, ordered by your deck priority, as plain text with
  minimal markup (`**bold**`, `_italic_`, `[...]` cloze) or cleaned HTML; media as a separate list;
  cursor pagination; `ETag` and `304`.
- `POST /exchange`: submit pending reviews, sync, and get the next batch in one request.
- `GET /stats`, `GET /decks`, `GET /media/{filename}` with `Range`.
- `POST /sync` and `GET /sync/status`: incremental collection and media sync; autosync
  `after_write` (debounced), `nightly`, or `off`.
- MCP at `/mcp/p/{profile}` for LLM agents (Claude Code, claude.ai, VS Code, Cursor): add and
  edit notes, search, review in chat, suspend or reschedule cards, move cards, stats, sync,
  and (with the `delete` scope) delete notes. Tools are filtered by token scope; claude.ai
  connectors sign in with OAuth against a consent page where you paste an Ankido token.
- `PATCH /notes`: edit fields, tags and attachments, with `expected_mod` against stale writes
  and a guard against edits that would drop media. `POST /notes/delete` (scope `delete`) backs
  the collection up first. `POST /cards/schedule` suspends, unsuspends, forgets or sets the due
  date without a review; `POST /cards/move`, `POST /decks`, `GET /tags`.
- `GET /models` and `GET /notes?query=`: note types with their fields, and note search with
  Anki's search syntax.
- AnkiConnect v6 shim at `POST /api/{profile}` so Yomitan, asbplayer and existing scripts keep
  working.
- Tokens with scopes `read`, `add`, `review`, `sync`, `delete`, `admin`; only argon2 hashes are stored;
  expiry and revocation.
- Rate limits per token and per profile, request size limit, operation timeouts, CORS allowlist,
  append-only audit log, Prometheus-format metrics, JSON logs with secret scrubbing.
- One process, one worker thread per profile, collections opened lazily and closed when idle.
- Multi-arch image (amd64, arm64), non-root, healthcheck.

## Quickstart

This runs Ankido with Docker Compose for one AnkiWeb account. The long version, including running
without Docker, is in [docs/quickstart.md](docs/quickstart.md).

1. Create the layout:

   ```sh
   mkdir -p ankido/config ankido/data ankido/secrets && cd ankido
   ```

2. Write `config/ankido.yaml`:

   ```yaml
   data_dir: /data
   profiles:
     alice:
       collection: /data/alice/collection.anki2
       credentials: /run/secrets/alice.env
       autosync: after_write
   ```

   A commented example with every key is in [`ankido.example.yaml`](ankido.example.yaml).

3. Write `secrets/alice.env` with the AnkiWeb login:

   ```sh
   printf 'ANKIWEB_USERNAME=alice@example.com\nANKIWEB_PASSWORD=change-me\n' > secrets/alice.env
   ```

4. The container runs as uid 1000 and refuses secret files that are readable by others:

   ```sh
   chmod 600 secrets/alice.env
   sudo chown -R 1000:1000 secrets data
   ```

5. Copy [`compose.yaml`](compose.yaml) from the repository, or write it:

   ```yaml
   services:
     ankido:
       image: ghcr.io/h1d/ankido:0.1
       restart: unless-stopped
       ports:
         - "127.0.0.1:8765:8765"
       volumes:
         - ./config:/config:ro
         - ./data:/data
       secrets:
         - alice.env
       environment:
         TZ: "UTC"   # set your zone: nightly sync time and per-day stats use it
       read_only: true
       tmpfs:
         - /tmp
       security_opt:
         - no-new-privileges:true

   secrets:
     alice.env:
       file: ./secrets/alice.env
   ```

   The image starts `ankido serve --bind 0.0.0.0` inside the container; the `ports` line publishes
   it on the host loopback only.

6. Start it and check the health endpoint:

   ```sh
   docker compose up -d
   curl -s http://127.0.0.1:8765/healthz
   ```

7. Create an admin token for yourself (the secret is printed once):

   ```sh
   docker compose exec ankido ankido token create --admin --name ops
   ```

8. Run the first sync. The local collection is empty, so this downloads your collection from
   AnkiWeb:

   ```sh
   curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8765/v1/p/alice/sync
   ```

   If the AnkiWeb account is brand new and no client has ever synced to it, AnkiWeb may demand a
   full upload instead. Ankido never uploads on its own and answers `409 sync_required_full` with
   `"required": "full_upload"`. Do it once, explicitly:

   ```sh
   curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
     http://127.0.0.1:8765/v1/p/alice/sync -d '{"force_full": "upload", "confirm": "alice"}'
   ```

9. Create a token for a device. `read,review` lets it fetch cards and grade them, nothing else:

   ```sh
   docker compose exec ankido ankido token create --profile alice --scopes read,review --name kitchen
   ```

10. Fetch the queue:

    ```sh
    curl -s -H "Authorization: Bearer $KITCHEN_TOKEN" \
      "http://127.0.0.1:8765/v1/p/alice/queue?limit=5"
    ```

## Three API surfaces

| | `/v1/p/{profile}/...` | `/mcp/p/{profile}` (MCP) | `/api/{profile}` (AnkiConnect shim) |
| --- | --- | --- | --- |
| Purpose | Primary contract; new features land here | LLM agents; the `/v1` operations as tools | Compatibility for Yomitan, asbplayer, existing scripts |
| Auth | `Authorization: Bearer` | `Authorization: Bearer`, or OAuth sign-in | `Authorization: Bearer` or legacy `key` in the body |
| Errors | HTTP status plus `{"error":{"code","message","retryable"}}` | Tool result with `isError` and the same JSON | HTTP 200 with a bare error string |
| Idempotency | `client_id` on notes and reviews | Same as `/v1` | None; re-sent `answerCards` grades twice |
| Efficiency | Cursor, `ETag`/`304`, `Range`, gzip, compact text | Compact text | Everything at once, raw HTML |

Reference: [docs/api.md](docs/api.md), [docs/mcp.md](docs/mcp.md) and
[docs/ankiconnect-shim.md](docs/ankiconnect-shim.md).

## Hard invariants

1. Anki owns scheduling. Intervals, ease and due dates come from `col.sched.answer_card`; the
   service and its clients never compute them.
2. One writer per collection. One worker thread per profile runs a serialized queue; reads are
   prioritised over writes, syncs run last.
3. No full-sync upload by default. When AnkiWeb demands a full sync, the request fails with
   `sync_required_full`. Forcing it is an admin-only call that requires `confirm`, takes a backup,
   and writes an audit entry.
4. Schema upgrades need permission (see the warning below).
5. No anonymous surface. Only `GET /healthz` answers without a token, and it carries no data
   (plus, once you enable MCP OAuth, the OAuth discovery documents and the consent page, which
   carry none either). The default bind is `127.0.0.1`.
6. Writes are idempotent by `client_id`.
7. Secrets never reach logs, error messages, metrics, or the audit log.
8. No telemetry. The service talks to AnkiWeb and to the media hosts you allowlist, nothing else.

## Warning: schema upgrades

The `anki` library bundled in the image can be newer than the version of Anki you use on your
other devices. If it is, the first time Ankido opens your collection it would upgrade the
collection's schema. Once that upgrade is synced, every other device of that account is forced
through a one-off full sync and has to pick a side. This is the main way to lose data with any
tool built on the Anki library.

Ankido therefore refuses to open such a collection and returns `schema_upgrade_required` until
you set `allow_schema_upgrade: true` on that profile. When you do, it takes a backup first. Read
[docs/schema-upgrade.md](docs/schema-upgrade.md) before setting that flag.

## Security defaults

- The service binds to `127.0.0.1`. Do not publish the port to a LAN or the internet. Put it
  behind a TLS reverse proxy or an identity-aware proxy (Caddy, nginx, Cloudflare Tunnel with
  Access); see [docs/deploy.md](docs/deploy.md).
- Give devices tokens with scopes `read,review`. Such a token can fetch cards and grade them, but
  cannot add, delete, or sync.
- Tokens are shown once at creation. Only an argon2 hash is stored. Revoke with
  `ankido token revoke <id>`.
- AnkiWeb credentials come from a `0600` file or environment variables. They are never baked into
  the image and are scrubbed from logs.
- Media fetched by URL must come from hosts in `media.url_allowlist`; paths must be under
  `media.path_allowlist`.

## Status and roadmap

Version 0.2. The `/v1` surface and the MCP tool set are the contract; changes to them follow
semver and are recorded in [CHANGELOG.md](CHANGELOG.md).

## Documentation

- [docs/quickstart.md](docs/quickstart.md): compose and bare-metal setup, config reference, CLI
- [docs/api.md](docs/api.md): `/v1` reference with `curl` examples and error codes
- [docs/ankiconnect-shim.md](docs/ankiconnect-shim.md): the AnkiConnect-compatible endpoint
- [docs/mcp.md](docs/mcp.md): MCP tools, client setup (Claude Code, claude.ai, VS Code,
  Cursor), OAuth
- [docs/clients.md](docs/clients.md): writing your own client, offline replay, `exchange`
- [docs/deploy.md](docs/deploy.md): reverse proxies, Cloudflare Tunnel, backups, updates, logs
- [docs/schema-upgrade.md](docs/schema-upgrade.md): the upgrade risk and the full-sync policy
- [docs/migration.md](docs/migration.md): moving from AnkiConnect or from full-sync scripts
- [docs/SPEC.md](docs/SPEC.md): the original requirements
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md)

Repository: `github.com/H1D/AnkiDo`. Image: `ghcr.io/h1d/ankido` (tags `latest`, `X.Y.Z`, `X.Y`,
and immutable `sha-<short>`). Ankido is not on PyPI; without Docker, install it from Git
([quickstart](docs/quickstart.md#without-docker)).

## License

AGPL-3.0-or-later. Ankido is built on the `anki` library, which is AGPL-3.0-or-later, so the
choice is made for us. In plain terms: running Ankido for yourself obliges you to nothing. If you
modify it and run it as a service for other people, you must offer those people the source code of
the version you run. See [LICENSE](LICENSE).

## Credits

Ankido was written from scratch. Its design reference is
[`formeo14/anki-connect-server`](https://github.com/formeo14/anki-connect-server) at commit
`57c0b064956f66b46a210bbf8159f85871224d21`; that project has no license, so no code was copied
from it. [`glechic/anki-connect-server`](https://github.com/glechic/anki-connect-server) is a
sibling project worth comparing. The heavy lifting (scheduling, sync, rendering) is done by the
[`anki`](https://github.com/ankitects/anki) library.

## Assets

| File | Use |
| --- | --- |
| `assets/logo.svg` | Logo mark |
| `assets/favicon.svg` | Favicon (adapts to dark mode) |
| `assets/favicon.ico` | Legacy favicon, 16/32/48 px |
| `assets/apple-touch-icon.png` | 180×180 touch icon |

The mark is an ensō, the open brush circle, drawn around a pair of flashcards. The front card is
seal-red and has a path running across it. *Dō* (道) means "way".
