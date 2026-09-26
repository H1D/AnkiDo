---
name: Bug report
about: Something does not behave as documented
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- One or two sentences. -->

## What you expected

<!-- Quote the relevant part of docs/api.md or another doc if it describes the expected behaviour. -->

## How to reproduce

<!-- The exact request (curl is ideal) and the exact response. Remove the token value; the
     8-character token id is fine to keep. -->

```sh
curl -sS -H "Authorization: Bearer akd_***" ...
```

```json
{"error": {...}}
```

## Environment

- Ankido version (`ankido --version` or image tag, e.g. `ghcr.io/h1d/ankido:0.1.0`):
- How it runs (compose, bare metal with uv/pipx, other):
- Host OS and architecture (e.g. Debian 12 on arm64 / Raspberry Pi 4):
- Reverse proxy in front, if any:
- Client (own code, Yomitan, asbplayer, script, ...):

## Logs

<!-- Relevant JSON log lines from stdout around the time of the problem. Ankido scrubs tokens
     and passwords, but check anyway before pasting. Never paste card content that is not yours
     to share. -->

```
```

## Sync state, if the problem involves sync

<!-- Output of GET /v1/p/{profile}/sync/status, and whether other devices of the account showed
     a full-sync prompt. -->
