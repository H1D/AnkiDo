# MCP

Ankido serves the [Model Context Protocol](https://modelcontextprotocol.io) at
`/mcp/p/{profile}`, one endpoint per profile. An agent (Claude Code, claude.ai, VS Code, Cursor,
anything that speaks MCP over HTTP) can then add notes, search the collection, run a review
session in chat and read stats, with the same rules as `/v1`: Anki owns scheduling, writes are
idempotent by `client_id`, every call is scoped and rate-limited, and nothing does a full sync.

- Transport: Streamable HTTP, stateless. Protocol `2026-07-28` clients send self-contained
  requests. Older clients (`2025-11-25` and earlier) still get the `initialize` handshake, but no
  session is kept between requests, so any request can land on any restart of the service.
- Requests are answered with plain JSON rather than an event stream.
- Auth: a static Ankido token in `Authorization: Bearer`, or OAuth for clients that can only
  sign in (claude.ai). See [Authentication](#authentication).

## Tools

The tool list a client sees depends on its token: a `read,review` token never sees `add_notes`.
Calls are checked again when they arrive, so an unlisted tool fails with `forbidden` too.

| Tool | Scope | Same as | What it does |
| --- | --- | --- | --- |
| `list_decks` | `read` | `GET /v1/p/{p}/decks` | Decks with hierarchy and new/learning/due counts. |
| `list_note_types` | `read` | `GET /v1/p/{p}/models` | Note types with field names in order and template names. |
| `search_notes` | `read` | `GET /v1/p/{p}/notes?query=` | Anki search syntax, newest first, fields as plain text. `limit`, `offset`. |
| `get_stats` | `read` | `GET /v1/p/{p}/stats` | Reviews per day (default 30 days), today's count, per-deck counts, `day_rollover_hour`. |
| `get_queue` | `read` | `GET /v1/p/{p}/queue` | Cards to study now: `q`, `a` as plain text, `next` intervals per grade. `decks`, `kinds`, `limit`, `cursor`. |
| `sync_status` | `read` | `GET /v1/p/{p}/sync/status` | Last sync, errors, whether a full sync is pending. |
| `add_notes` | `add` | `POST /v1/p/{p}/notes` | Add notes, deduplicated on the first field. Media as base64 `data` or allowlisted `url`. |
| `submit_reviews` | `review` | `POST /v1/p/{p}/reviews` | Grade cards 1 to 4; Anki computes the next interval. |
| `sync` | `sync` | `POST /v1/p/{p}/sync` | Incremental sync with AnkiWeb. |

Differences from `/v1`:

- `add_notes` does not accept `path` media. A remote agent has no business reading files on
  the server.
- `get_queue` always returns compact plain text; `search_notes` always returns plain-text fields.
- `exchange`, media download, backups, forced full syncs and the admin endpoints are not
  exposed. An `admin` token sees the same nine tools as a `read,add,review,sync` token.

Tool results carry `structuredContent` (the same JSON the `/v1` endpoint returns) and a text
copy. Failures come back as a tool result with `isError: true` whose text is the usual error
body, so an agent can read `code` and `retryable`:

```json
{"error": {"code": "deck_not_found", "message": "deck 'Nope' not found", "retryable": false}}
```

Per-item outcomes (a duplicate note, a rejected review) are not errors; they are in `results`,
exactly as in `/v1`.

## Prompt

`review_session` (optional argument `deck`) tells the agent how to quiz you: show the question
only, wait for your answer, reveal it, agree on a grade, submit grades in small batches with
unique `client_id`s, and never compute intervals itself. In clients that list prompts it shows
up as a command (in Claude Code: `/mcp__ankido__review_session`).

## Authentication

### Static token

This works with every client that can send a header, and needs no extra configuration.

```sh
ankido token create --profile alice --scopes read,add,review --name claude-code
```

Claude Code:

```sh
claude mcp add --transport http ankido https://anki.example.com/mcp/p/alice \
  --header "Authorization: Bearer akd_…"
```

VS Code (`.vscode/mcp.json`), prompting for the token instead of storing it:

```json
{
  "inputs": [
    {"type": "promptString", "id": "ankido-token", "description": "Ankido token", "password": true}
  ],
  "servers": {
    "ankido": {
      "type": "http",
      "url": "https://anki.example.com/mcp/p/alice",
      "headers": {"Authorization": "Bearer ${input:ankido-token}"}
    }
  }
}
```

Cursor (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "ankido": {
      "url": "https://anki.example.com/mcp/p/alice",
      "headers": {"Authorization": "Bearer akd_…"}
    }
  }
}
```

On the same machine as Ankido, `http://127.0.0.1:8765/mcp/p/alice` works too.

### OAuth

claude.ai and Claude Desktop add remote servers as "custom connectors", which sign in with OAuth
and cannot send a fixed header. For them Ankido runs a small authorization server of its own.

It needs two things:

1. **`server.public_url`** in `ankido.yaml`: the external https origin, with no path.

   ```yaml
   server:
     public_url: https://anki.example.com
   ```

2. **A public https address.** claude.ai connects from Anthropic's cloud, not from your
   computer, so the URL must be reachable from the internet through a TLS reverse proxy or a
   tunnel ([deploy.md](deploy.md)). A LAN-only or VPN-only Ankido cannot be a claude.ai connector;
   use a static token from a local client instead.

Then, in claude.ai: Settings → Connectors → Add custom connector, and enter
`https://anki.example.com/mcp/p/alice` (no trailing slash: clients compare it with the
`resource` Ankido announces). Claude Code works the same way if you add the server
without a header and run `/mcp` to sign in.

What happens next:

1. The client calls the endpoint, gets `401` with a pointer to
   `/.well-known/oauth-protected-resource/mcp/p/alice`, and discovers Ankido's authorization
   server from there.
2. It registers itself: through a Client ID Metadata Document (its `client_id` is an https URL
   Ankido fetches) or through Dynamic Client Registration (`POST /oauth/register`).
3. Your browser opens Ankido's consent page. It shows which client asks for which profile and
   where you will be sent afterwards. Tick the scopes to grant and **paste an Ankido token for
   that profile** (create one with `ankido token create` as above). Ankido has no user
   accounts; the token is the proof that you own the profile.
4. The client receives its own tokens: an access token valid for one hour and a refresh token
   valid for 90 days that is replaced at every use (each use also extends the 90 days).

Rules for what the client gets:

- At most the scopes of the token you pasted, and never `admin`. An admin token can grant
  `read`, `add`, `review` and `sync`.
- Only for the one URL it asked for. An OAuth token does not work on `/v1`, on the AnkiConnect
  shim, or on another profile's MCP endpoint.
- It never outlives the pasted token: if that token expires or is revoked, the client is
  disconnected.

The connection shows up in `ankido token list` as a token named `oauth:<client name>`. To
disconnect one client, revoke that id. To disconnect everything approved with a token (say you
pasted it somewhere you shouldn't have), revoke the pasted token:

```sh
ankido token list --profile alice
ankido token revoke 3f9c01ab
```

Clients can also revoke their own tokens at `POST /oauth/revoke`.

The consent page is plain HTML with no JavaScript, protected by a CSRF token, and cannot be
framed. Failed attempts to paste a token are rate-limited per client address
(`rate_limits.oauth`) and written to the audit log as `oauth_consent` / `bad_token`.

### When OAuth is not set up

Without `server.public_url`, static tokens keep working and the OAuth endpoints answer with an
error that says what to change. A client that connects without a token gets:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="ankido", error_description="OAuth for MCP is not configured on this server: server.public_url is not set"
```

```json
{
  "error": {
    "code": "oauth_not_configured",
    "message": "OAuth for MCP is not configured on this server: server.public_url is not set",
    "retryable": false,
    "details": {
      "fix": [
        "set server.public_url in ankido.yaml to the external HTTPS origin clients use, e.g. https://anki.example.com (no path)",
        "restart ankido",
        "or skip OAuth: create a token with `ankido token create --profile <name> --scopes read,add,review` and send it as 'Authorization: Bearer <token>'"
      ],
      "docs": "https://github.com/H1D/AnkiDo/blob/main/docs/mcp.md#oauth"
    }
  }
}
```

The same code comes back (with `404`) from the `.well-known` and `/oauth/*` endpoints. The
service logs a warning at startup, and `GET /v1/admin/profiles` reports `"oauth":
"not_configured"`.

`oauth_public_url_mismatch` means `public_url` is set but OAuth still cannot work, and
`details` says why:

- `public_url` uses `http://`. MCP clients refuse OAuth over plain http (localhost excepted).
- The request arrived for another host than `public_url`. `details.expected` and
  `details.seen` show both. Either the client uses a different address (connect it to
  `public_url`), `public_url` is wrong, or the reverse proxy rewrites the `Host` header. Caddy
  forwards it by default; nginx needs `proxy_set_header Host $host`. Alternatively list the
  proxy in `server.trusted_proxies`, and Ankido will read `X-Forwarded-Host`.

## Trying it by hand

MCP is JSON-RPC over `POST`, so `curl` can call a tool without a handshake:

```sh
curl -s https://anki.example.com/mcp/p/alice \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"get_queue","arguments":{"limit":3}}}'
```

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) works too: transport
"Streamable HTTP", the URL above, and an `Authorization` header.
