# Security policy

## Reporting a vulnerability

Do not open a public issue for security problems.

Use GitHub's private vulnerability reporting on the repository: open
`https://github.com/H1D/AnkiDo/security/advisories/new` (Security tab, "Report a
vulnerability"). The report is visible only to the maintainer until a fix is published.

Include the version (`ankido --version` or the image tag), how the service is deployed, steps to
reproduce, and what an attacker gains. You will get an acknowledgement within seven days. Fixes
are published as a patch release with a `CHANGELOG.md` entry and, where it helps others, a
GitHub advisory that credits you unless you prefer otherwise.

## What is in scope

Anything that breaks the guarantees the documentation makes:

- reaching any data without a valid token, or with a token that lacks the scope
- recovering a token or an AnkiWeb credential from logs, error responses, metrics, the audit
  log, or the state database
- crossing profiles: a token bound to one profile affecting another
- server-side request forgery or path traversal through media attachments or `media/{filename}`
- bypassing the full-sync or schema-upgrade gates so that data on another device is overwritten
  without an explicit admin action
- denial of service that a rate-limited, authenticated client should not be able to cause

## What is out of scope

- Deployments that ignore the documented defaults: a port published to a network without TLS,
  tokens with broader scopes than needed, `ANKIDO_ALLOW_INSECURE_SECRETS=1` outside development.
- Vulnerabilities in AnkiWeb, in the `anki` library, or in the sync protocol itself. Report those
  to the Anki project.
- The AnkiConnect shim accepting the token in the request body: that is a documented property of
  the AnkiConnect dialect, mitigated by using the `Authorization` header and TLS.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |
| earlier | no |

Only the latest minor release receives fixes. Pin an `X.Y` or `sha-<short>` image tag and read the
changelog before upgrading.
