# Deployment

Ankido binds to `127.0.0.1` by default and the Docker image publishes nothing on its own. Every
path to the outside goes through something in front of it that terminates TLS and, ideally, also
authenticates the person. This page covers those fronts, plus backups, updates, resources, logs
and metrics.

## Rules

- Never publish port 8765 on a LAN or the internet. Tokens are bearer secrets; without TLS they
  travel in the clear, and the AnkiConnect shim accepts them in the request body.
- One Ankido process per data directory. Two processes (or a process plus `ankido sync`) on the
  same collection will corrupt it or fail to open it.
- Keep `server.bind` at `127.0.0.1` on bare metal. In Docker the image starts
  `serve --bind 0.0.0.0` so that the port can be published, and `compose.yaml` publishes it as
  `127.0.0.1:8765:8765`, so it is still only reachable from the host.

## Behind a reverse proxy

### Caddy

Caddy obtains and renews certificates by itself.

```
anki.example.com {
    reverse_proxy 127.0.0.1:8765
    request_body {
        max_size 16MB
    }
}
```

Caddy sets `X-Forwarded-For` automatically. To have Ankido log the real client address instead
of `127.0.0.1`, set `server.trusted_proxies: ["127.0.0.1"]` (or the address the container sees:
for Docker on Linux that is the bridge gateway, typically `172.17.0.1` or the compose network's
gateway, visible in the `client` field of the request log before you configure anything).

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name anki.example.com;

    ssl_certificate     /etc/letsencrypt/live/anki.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/anki.example.com/privkey.pem;

    client_max_body_size 16m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 620s;   # POST /sync with wait=true can take up to 600 s
    }
}

server {
    listen 80;
    server_name anki.example.com;
    return 301 https://$host$request_uri;
}
```

`proxy_read_timeout` matters for `POST /v1/p/{profile}/sync`: a first download of a large
collection with media can run for minutes. If you keep the default 60 s, use `{"wait": false}`
and poll `GET sync/status`.

### Restricting who can reach it

Tokens authenticate clients, but the port is still a public surface that can be probed and
rate-limited into `429`s by strangers. Options, from simplest to strongest:

- Allowlist source addresses at the proxy (`remote_ip` matcher in Caddy, `allow`/`deny` in
  nginx) if your devices have fixed addresses or come through a VPN.
- Put the proxy on a VPN or overlay network (WireGuard, Tailscale) and do not expose it publicly
  at all. Devices then need a VPN client; microcontrollers usually cannot, which is where the next
  option comes in.
- Use an identity-aware proxy (below) and exempt only the paths your devices need, with a
  service token.

## Cloudflare Tunnel and Access

With a tunnel, no inbound port is open at all. `cloudflared` runs next to Ankido and connects out.

1. Create a tunnel and route a hostname to `http://127.0.0.1:8765` (or `http://ankido:8765` if
   `cloudflared` runs as a second compose service on the same network; then Ankido's `ports`
   entry can go away entirely).
2. Add a Cloudflare Access application for that hostname. Human users log in through Access;
   devices and scripts use an Access service token (`CF-Access-Client-Id` /
   `CF-Access-Client-Secret` headers) in addition to their Ankido bearer token.
3. Set `server.trusted_proxies` to the address `cloudflared` connects from as seen by Ankido
   (`127.0.0.1` on bare metal, the compose network address in Docker). Ankido then reads
   `CF-Connecting-IP`, and falls back to `X-Forwarded-For`, for the `client` field in the request
   log.

`trusted_proxies` affects logging only. Rate limits are per token and per profile, not per
address, so a wrong value cannot let anyone bypass them; it can only make the log show the proxy's
address instead of the client's. Entries are exact addresses; they are also handed to uvicorn's
proxy-header handling.

## CORS for browser clients

Browser extensions and web readers need `server.cors_origins` set to their origins, for example
`["chrome-extension://<id>", "https://reader.example.com"]`. Without it, the browser blocks the
request before it reaches Ankido. With it, Ankido allows `GET`, `POST`, `OPTIONS`, the headers
`Authorization`, `Content-Type`, `If-None-Match`, and exposes `ETag`. Do not use `"*"` on an
authenticated API.

## Raspberry Pi and other arm64 hosts

The image is built for `linux/amd64` and `linux/arm64`; `docker compose pull` picks the right
one. A Pi 4 with 2 GB is enough for a few profiles. Things worth adjusting:

- `server.idle_close_seconds` (default 600): a closed collection uses no memory. Lower it if
  RAM is tight, raise it if the first request after idle is too slow for your device.
- Keep `data_dir` on something better than the SD card if you can (USB SSD). The collection is a
  SQLite database in WAL mode, and every review is a write.
- `TZ` in the container, so `nightly_at` fires when you expect.

32-bit ARM (`armv7`) is not supported: the `anki` wheels do not exist for it.

## Backups and retention

Ankido's own backups (see [schema-upgrade.md](schema-upgrade.md#backups)) are copies of the
collection file in `<data_dir>/<profile>/backups/`, pruned to `backups.keep` (default 10). They
protect against Ankido's own risky operations, not against losing the disk.

For that, copy `data_dir` somewhere else on a schedule. It contains:

| Path | Contents | Notes |
| --- | --- | --- |
| `ankido.db` | tokens (hashes), idempotency journal, audit log | Small. Losing it means re-issuing tokens and losing 90 days of replay protection. |
| `<profile>/collection.anki2` (and `-wal`, `-shm`) | the collection | Copy it with the service stopped, or take an application-level backup first (`POST /v1/p/{profile}/backup`) and copy the backup file, which is a checkpointed, consistent copy. |
| `<profile>/collection.media/` | media files | Can be re-fetched from AnkiWeb, but a copy saves time. |
| `<profile>/backups/` | Ankido's backups | |
| `<profile>/sync.key` | cached AnkiWeb sync key, mode 0600 | Treat as a secret. Safe to lose: Ankido logs in again with the password. |

A simple approach with the service running:

```sh
docker compose exec ankido ankido token list >/dev/null   # sanity check the container is up
curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://anki.example.com/v1/p/alice/backup
rsync -a --delete data/ /mnt/backup/ankido/
```

Remember that AnkiWeb keeps a full copy of everything that has synced. The local data that is
unique to Ankido is whatever has not synced yet, the tokens, and the journal.

## Updating

Tags: `latest` (moves), `X.Y` (moves within a minor), `X.Y.Z` (fixed), `sha-<short>`
(immutable). For something that holds your collection, pin `X.Y` or `sha-...` and read
[CHANGELOG.md](../CHANGELOG.md) before moving. Pay attention to entries that mention the `anki`
library version: a library that writes a newer schema brings the situation described in
[schema-upgrade.md](schema-upgrade.md).

```sh
docker compose pull
docker compose up -d
docker compose logs --since 2m ankido
```

Shutdown is graceful: SIGTERM makes each worker flush a pending `after_write` sync and close its
collection. Give it time (`stop_grace_period: 60s`); Docker's default 10 s can cut a sync short,
which is safe (the next sync resumes) but wastes the work. Run `ankido check-config` from the new
image before relying on it if the config format changed.

Downgrading the image to an older `anki` library after a schema upgrade does not work; restore
the `pre-schema-upgrade` backup instead.

## Resources

- Memory: a closed profile costs almost nothing; an open collection is typically 100 to 300 MB
  depending on its size, under 400 MB in normal use. Budget for the number of profiles you expect
  to be open at once (`idle_close_seconds` controls that).
- CPU: idle between requests. Rendering a queue of 60 cards, a batch of reviews, or a note with
  audio each take well under a second on a Pi 4 once the collection is open. The first request
  after idle pays for opening the collection.
- Disk: the collection plus media, backups (`backups.keep` copies of the collection), and
  `ankido.db`, which grows with the audit log (one row per write request) and is pruned of
  journal entries older than 90 days at startup.
- Network: only to AnkiWeb (or `sync_endpoint`) and to hosts in `media.url_allowlist`.

## Logs

Everything goes to stdout as one JSON object per line:

```json
{"ts":"2026-09-26T10:15:00.123Z","level":"INFO","logger":"ankido.http","msg":"request","method":"POST","path":"/v1/p/alice/reviews","status":200,"ms":41,"client":"203.0.113.7","token_id":"3f9a1c2b"}
{"ts":"2026-09-26T10:15:31.002Z","level":"INFO","logger":"ankido.supervisor","msg":"sync done","profile":"alice","outcome":"merged","local_replaced":false,"server_message":"","media":"synced","duration_ms":1840}
```

`server.log_level` (default `INFO`) controls verbosity; `DEBUG` adds one line per worker
operation with queue wait times. `uvicorn.access`, `httpx` and `httpcore` are kept at
`WARNING`. Passwords, sync keys and anything shaped like a token are replaced by `***` before a
line is written, including inside tracebacks. `token_id` is the public short id, which is enough
to correlate with `ankido token list` and the audit log.

`docker compose logs -f ankido` or your log driver of choice; nothing is written to files.

## Metrics

`GET /v1/admin/metrics` returns Prometheus text format and needs an admin token. Scrape it with
a bearer credential:

```yaml
scrape_configs:
  - job_name: ankido
    metrics_path: /v1/admin/metrics
    scheme: https
    authorization:
      credentials_file: /etc/prometheus/ankido.token
    static_configs:
      - targets: ["anki.example.com"]
```

Metrics available: `ankido_http_requests_total{route,status}`,
`ankido_http_request_seconds` histogram `{route}` (buckets from 5 ms to 10 s),
`ankido_sync_total{profile,outcome}`. Counters reset when the process restarts. Route labels are
templates (`/v1/p/{profile}/queue`), so profile names are not exposed through metrics. Useful
alerts: `ankido_sync_total{outcome="error"}` increasing, and `status="5.."` on any route.
