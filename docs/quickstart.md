# Quickstart

This page gets Ankido running, with Docker or without, explains every configuration key, and
documents the `ankido` command line.

## What you need

- An AnkiWeb account per profile (the username is the account email; the password is the
  AnkiWeb password, not an API key).
- Docker with the Compose plugin, or Python 3.12+ with `uv` or `pipx`.
- Somewhere to keep state. Everything Ankido writes lives under `data_dir` (default `/data`):
  - `ankido.db`: tokens (argon2 hashes only), the idempotency journal, and the audit log;
  - `<profile>/backups/`: collection backups;
  - `<profile>/sync.key`: the cached AnkiWeb sync key, mode `0600`, so the password is only used
    at first login and after a key rejection;
  - the collection itself at the `collection` path you configure (conventionally
    `<data_dir>/<profile>/collection.anki2`, with its `collection.media/` directory next to it).

## With Docker Compose

1. Create a directory with `config/`, `data/` and `secrets/` subdirectories.

2. `config/ankido.yaml`:

   ```yaml
   data_dir: /data
   profiles:
     alice:
       collection: /data/alice/collection.anki2
       credentials: /run/secrets/alice.env
       autosync: after_write
   ```

   Start from [`ankido.example.yaml`](../ankido.example.yaml) if you want every key spelled out.
   The image's command is `ankido serve --bind 0.0.0.0`, which overrides `server.bind` inside the
   container; that is safe because Compose publishes the port on the host loopback only (below).
   On bare metal the default `127.0.0.1` applies.

3. `secrets/alice.env`:

   ```
   ANKIWEB_USERNAME=alice@example.com
   ANKIWEB_PASSWORD=change-me
   ```

   The file must be mode `0600` (or `0400`) and readable by uid 1000, the user the container runs
   as. `chmod 600 secrets/alice.env && sudo chown 1000:1000 secrets/alice.env`. Ankido refuses
   files that group or others can read; `ANKIDO_ALLOW_INSECURE_SECRETS=1` disables that check for
   local development only. If your Compose version mounts file secrets with a wider mode and
   `check-config` complains, bind-mount the file instead:
   `- ./secrets/alice.env:/run/secrets/alice.env:ro` under `volumes`.

   Instead of a file you can pass the login through the environment:
   `ANKIDO_PROFILE_ALICE_USERNAME` and `ANKIDO_PROFILE_ALICE_PASSWORD` (profile name uppercased,
   `-` replaced by `_`). Environment variables take precedence over the file.

4. Give the data directory to uid 1000: `sudo chown -R 1000:1000 data`.

5. `compose.yaml`, as shipped in the repository root:

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
         TZ: "UTC"
       read_only: true
       tmpfs:
         - /tmp
       security_opt:
         - no-new-privileges:true

   secrets:
     alice.env:
       file: ./secrets/alice.env
   ```

   `TZ` matters: `nightly_at` and the date labels in `reviewed_by_day` use the container's local
   time. Consider `stop_grace_period: 60s`: on shutdown the worker flushes a pending `after_write`
   sync and closes the collection, and waits up to 30 s per profile to do so.

   The image runs as user `ankido` (uid 1000), listens on 8765, and has a healthcheck that calls
   `GET /healthz`. Config is read from `/config/ankido.yaml`; override the path with the
   `ANKIDO_CONFIG` environment variable or `--config`.

6. `docker compose up -d`, then `docker compose logs -f ankido`. Logs are JSON lines. You should
   see `worker started` for each profile. Nothing has touched AnkiWeb yet: collections open
   lazily on the first request.

7. Validate the config from inside the container:

   ```sh
   docker compose exec ankido ankido check-config
   ```

8. Create an admin token and run the first sync:

   ```sh
   docker compose exec ankido ankido token create --admin --name ops
   curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8765/v1/p/alice/sync
   ```

   With an empty local collection (zero notes, zero cards), Ankido downloads the account's
   collection from AnkiWeb; the response has `"outcome": "downloaded"` and
   `"local_replaced": true`. This is the only case where Ankido replaces local data on its own;
   see [schema-upgrade.md](schema-upgrade.md#full-sync-policy). Media files are fetched in the
   background; `"media": "in_progress"` means the collection sync finished but media was still
   downloading when the 180 s wait ran out.

   If the AnkiWeb account is brand new and no client has ever synced to it, AnkiWeb may demand a
   full upload even though both sides are empty. Ankido refuses implicit full uploads and returns
   `409 sync_required_full` with `"details": {"required": "full_upload"}`. Run the upload once,
   explicitly, with the admin token:

   ```sh
   curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
     http://127.0.0.1:8765/v1/p/alice/sync -d '{"force_full": "upload", "confirm": "alice"}'
   ```

   or, with the service stopped, `ankido sync alice --force-full upload --yes`. Either way a
   backup is taken first unless the local collection is empty, and the action is written to the
   audit log.

9. Create device tokens with the narrowest scopes that work, and start using the API:

   ```sh
   docker compose exec ankido ankido token create --profile alice --scopes read,review --name kitchen --expires 1y
   curl -s -H "Authorization: Bearer $KITCHEN_TOKEN" "http://127.0.0.1:8765/v1/p/alice/queue?limit=5"
   ```

Before exposing the port anywhere else, read [deploy.md](deploy.md).

## Without Docker

Ankido is not published on PyPI; install a release tag straight from GitHub. Replace `vX.Y.Z`
with a tag from the [releases page](https://github.com/H1D/AnkiDo/releases):

```sh
uv tool install git+https://github.com/H1D/AnkiDo@vX.Y.Z
# or: pipx install git+https://github.com/H1D/AnkiDo@vX.Y.Z
```

To update, run the same command with the new tag and `--force` (`uv tool install --force …`,
`pipx install --force …`).

Write `ankido.yaml` in the working directory (or point `ANKIDO_CONFIG` at it, or pass
`--config`). Set `data_dir` to a directory the service user can write; nothing else needs root.

```yaml
data_dir: /var/lib/ankido
profiles:
  alice:
    collection: /var/lib/ankido/alice/collection.anki2
    credentials: /etc/ankido/alice.env
```

Then:

```sh
ankido check-config
ankido serve               # binds 127.0.0.1:8765 by default; --bind and --port override
```

Config lookup order: `--config`, then `ANKIDO_CONFIG`, then `/config/ankido.yaml`, then
`./ankido.yaml`.

A minimal systemd unit:

```ini
[Unit]
Description=Ankido
After=network-online.target

[Service]
User=ankido
Environment=ANKIDO_CONFIG=/etc/ankido/ankido.yaml
ExecStart=/home/ankido/.local/bin/ankido serve
Restart=on-failure
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

## Configuration reference

The file is YAML. Unknown keys anywhere are an error, so typos fail at startup instead of being
ignored. Nothing secret goes in the file.

### Top level

| Key | Default | Meaning |
| --- | --- | --- |
| `data_dir` | `/data` | Root for `ankido.db`, per-profile backups and sync keys. |
| `server` | see below | HTTP server settings. |
| `media` | see below | Limits for media attached to notes. |
| `profiles` | required | Map of profile name to profile settings. At least one. Names match `^[a-z0-9][a-z0-9_-]{0,31}$`. |

### `server`

| Key | Default | Meaning |
| --- | --- | --- |
| `bind` | `127.0.0.1` | Address to listen on. Keep the default on bare metal. The Docker image passes `--bind 0.0.0.0` on its command line, which takes precedence, so the container is reachable on the published port. |
| `port` | `8765` | TCP port, 1 to 65535. |
| `trusted_proxies` | `[]` | Peer addresses whose `X-Forwarded-For` / `CF-Connecting-IP` / `X-Forwarded-Host` headers are believed. Used for the `client` field in request logs, the per-address OAuth rate limit and the `public_url` host check (with `X-Forwarded-Proto`). Exact addresses or CIDR ranges. |
| `cors_origins` | `[]` | Browser origins allowed to call the API. Empty disables CORS entirely. When set, methods `GET`, `POST`, `OPTIONS` and headers `Authorization`, `Content-Type`, `If-None-Match` are allowed and `ETag` is exposed. |
| `max_body_bytes` | `16777216` (16 MiB) | Requests with a larger `Content-Length` are rejected with `413 payload_too_large`. Minimum 1024. |
| `operation_timeout_seconds` | `30` | How long a request waits for the profile worker before failing with `503 profile_busy`. Sync waits up to 600 s, backup up to 300 s regardless. |
| `idle_close_seconds` | `600` | Close an open collection after this much inactivity. `0` keeps collections open. |
| `rate_limits.read` | `{per_minute: 600, burst: 120}` | Token bucket for read operations. |
| `rate_limits.write` | `{per_minute: 120, burst: 40}` | Token bucket for notes, reviews, exchange and shim writes. |
| `rate_limits.sync` | `{per_minute: 6, burst: 3}` | Token bucket for sync requests. |
| `rate_limits.oauth` | `{per_minute: 20, burst: 10}` | Token bucket for the OAuth consent form, registration and token endpoints, per client address. |
| `log_level` | `INFO` | Python log level name. |
| `public_url` | `null` | External https origin (no path), e.g. `https://anki.example.com`. Needed only for MCP OAuth, i.e. claude.ai connectors; see [mcp.md](mcp.md#oauth). |

Each of `read`, `write` and `sync` applies twice: once per token and once per profile. Whichever
is exhausted first produces `429 rate_limited` with a `Retry-After` header. MCP tool calls use
the same buckets as the matching `/v1` endpoints.

### `media`

| Key | Default | Meaning |
| --- | --- | --- |
| `url_allowlist` | `[]` | Hostnames media may be fetched from with `url`. `*.example.com` matches subdomains and the bare domain. IP literals are never allowed. Redirects are not followed. Empty means `url` is refused. |
| `max_bytes` | `8388608` (8 MiB) | Maximum size of one attachment, whatever its source. |
| `fetch_timeout_seconds` | `15` | HTTP timeout for `url` fetches. |
| `path_allowlist` | `[]` | Directories under which `path` attachments must live. Paths must be absolute. Empty means `path` is refused. |

### `profiles.<name>`

| Key | Default | Meaning |
| --- | --- | --- |
| `collection` | required | Absolute path of the `.anki2` file. Created on first open if missing. Media lives in the sibling `<stem>.media/` directory. |
| `credentials` | `null` | Path of a `0600` env file with `ANKIWEB_USERNAME` and `ANKIWEB_PASSWORD`. Without credentials (and without the `ANKIDO_PROFILE_<NAME>_*` variables) the profile works locally and sync is disabled. |
| `autosync` | `after_write` | `after_write`: sync some seconds after a successful write, debounced. `nightly`: once a day at `nightly_at`. `off`: only when `POST /sync` is called. A bare YAML `off` (which YAML reads as `false`) is accepted. |
| `after_write_debounce_seconds` | `30` | Quiet period after the last write before an `after_write` sync starts. |
| `nightly_at` | `03:30` | `HH:MM`, 24-hour, container local time. |
| `allow_schema_upgrade` | `false` | Permit opening a collection whose schema is older than the library's, after a backup. Read [schema-upgrade.md](schema-upgrade.md) first. |
| `sync_endpoint` | `null` | Custom sync server URL for self-hosted sync servers. `null` means AnkiWeb. |
| `media_sync` | `true` | Sync media as well as the collection. |
| `backups.keep` | `10` | Number of backups to retain per profile; older ones are deleted after each new backup. Minimum 1. |

A full example with comments is in [`ankido.example.yaml`](../ankido.example.yaml).

## Command line

Every command reads the config first. `--config PATH` (or `-c`) and `--version` are global.

### `ankido serve [--bind ADDR] [--port N]`

Run the HTTP service. `--bind` and `--port` override `server.bind` and `server.port`. Logs go to
stdout as JSON. Stop it with SIGTERM or Ctrl-C; a pending `after_write` sync is flushed and all
collections are closed before exit.

### `ankido token create`

```
ankido token create --profile alice --scopes read,review --name kitchen --expires 90d
ankido token create --admin --name ops
ankido token create --profile alice --scopes admin --name alice-admin
```

| Flag | Meaning |
| --- | --- |
| `--profile NAME` | Profile the token is bound to. Required unless `--admin`. Must exist in the config. |
| `--scopes LIST` | Comma-separated subset of `read`, `add`, `review`, `sync`, `admin`. Default `read,review`. `admin` on a profile-bound token grants every scope on that profile plus the admin endpoints for it. |
| `--name TEXT` | Free-text label shown in `token list`. |
| `--expires DURATION` | `30d`, `12h`, `1y`, etc. (units `s`, `m`, `h`, `d`, `y`). Default: never. |
| `--admin` | Create a global admin token: no profile binding, scope `admin`, valid on every profile. `--profile` is ignored. |

The command prints the token id, profile, scopes, expiry, and then the secret. The secret has the
form `akd_<id>_<random>` and is shown exactly once; only its argon2 hash is stored. The `<id>` part
appears in logs and the audit log, the random part never does.

### `ankido token list [--profile NAME]`

Table of tokens with id, profile, scopes, name, expiry, last use and state (`active`, `expired`,
`revoked`).

### `ankido token revoke ID`

Revoke by id. Takes effect on the next request that uses the token. Exit status 1 if there was
no active token with that id.

### `ankido profile list`

One line per configured profile: name, collection path, autosync mode.

### `ankido check-config`

Loads the config, resolves credentials for every profile (checking file modes), reports whether
each collection file exists, and exits 0 if everything is usable. Run it after every config
change. It prints AnkiWeb usernames, never passwords.

### `ankido sync PROFILE [--force-full upload|download --yes]`

Sync one profile from the command line. The service must not be running: an Anki collection has
exactly one writer. Prints the sync result as JSON. `--force-full` is the same admin escape hatch
as the API's `force_full` and needs `--yes`; a backup is taken first and the audit log gets an
entry with `token_id` `cli`.

### `ankido backup PROFILE`

Copy the collection into `<data_dir>/<profile>/backups/collection-<timestamp>-manual.anki2`,
prune to `backups.keep`, print the path. The service must not be running; while it runs, use
`POST /v1/p/{profile}/backup` with an admin token instead.

## Exit codes

`0` success, `1` a check or revoke found a problem, `2` bad arguments or unreadable config, `130`
interrupted.
