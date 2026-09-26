---
name: Feature request
about: Something the /v1 API, the CLI, or the config should do
title: ""
labels: enhancement
assignees: ""
---

## Problem

<!-- What are you trying to do, and what stops you today? Concrete beats abstract: "my e-paper
     reviewer needs X because Y". -->

## Proposed change

<!-- Endpoint, parameter, config key, CLI flag. Sketch the request and response if it is an API
     change. -->

## Which surface

- [ ] `/v1` API
- [ ] CLI or configuration
- [ ] Deployment / image
- [ ] AnkiConnect shim (note: the shim gets missing actions and bug fixes, not new concepts;
      new features go to `/v1`)

## Interaction with the hard invariants

<!-- docs/SPEC.md section 4: Anki owns scheduling, one writer per collection, no implicit full
     upload, schema upgrades gated, no anonymous surface, idempotent writes, no secrets in logs,
     no telemetry. Does the proposal touch any of them? -->

## Alternatives you considered

<!-- Including "do it client-side" and "use the shim". -->
