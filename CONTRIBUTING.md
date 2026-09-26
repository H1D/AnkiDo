# Contributing

Thanks for looking. Ankido is small and the bar for a contribution is low: a clear problem, a
focused change, tests for what changed.

## Development setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/H1D/AnkiDo.git
cd AnkiDo
uv sync                 # creates .venv with the dev group (pytest, ruff, pyright, ...)
uv run pytest           # unit and integration tests against throwaway collections
uv run ruff check .     # lint
uv run ruff format .    # format (line length 100)
uv run pyright          # strict mode, src and tests
```

To run the service from the checkout:

```sh
cp ankido.example.yaml ankido.yaml           # edit data_dir and profiles
ANKIDO_ALLOW_INSECURE_SECRETS=1 uv run ankido serve
```

`ANKIDO_ALLOW_INSECURE_SECRETS=1` skips the 0600 check on credential files. Use it on your
laptop, never on a server.

The Docker image builds with `docker build -t ankido .`; `compose.yaml` has a commented
`build: .` line for running the local build.

## Tests

- `uv run pytest` runs the unit and integration tests. They create temporary collections and
  never touch AnkiWeb.
- Coverage: `uv run pytest --cov=ankido --cov-report=term-missing`. New code should be at least
  80% covered; the CI check is on the diff, not the whole tree.
- Fixtures must carry the HTML shapes Anki actually produces (`<style>` blocks,
  `[anki:play:...]`, cloze spans, `<hr id=answer>`), but they must be synthetic. Do not put cards
  from anyone's real collection in the repository, not even one.
- Anything touching reviews needs an idempotency case (same `client_id` twice yields `duplicate`
  and the interval is unchanged).
- Anything touching the shim needs a case in the shim regression test.

### End-to-end test against AnkiWeb

`tests/test_e2e.py` exercises the real thing: add a note, sync, see it in the queue, grade it,
sync again, and confirm the grade by doing a fresh full download into a second, empty profile.
It is skipped automatically unless `ANKIDO_E2E_ENV` points at an env file with credentials:

```sh
mkdir -p .e2e
printf 'ANKIWEB_USERNAME=throwaway@example.com\nANKIWEB_PASSWORD=...\n' > .e2e/env
chmod 600 .e2e/env
ANKIDO_E2E_ENV=.e2e/env uv run pytest tests/test_e2e.py
```

Use a throwaway AnkiWeb account created for this purpose. Never your own: the test performs full
syncs and can wipe the account's collection. `.e2e/` and `*.env` are gitignored; keep it that way.
In CI the job runs only on pushes to `H1D/AnkiDo` where the account secrets exist, never on
forks or pull requests, so run it locally before submitting anything that touches sync.

## Code

- Python 3.12, type-annotated, `pyright` strict must pass. `ruff` must pass with the rule set in
  `pyproject.toml`.
- Every new file starts with `# SPDX-License-Identifier: AGPL-3.0-or-later`.
- Everything that touches a collection runs on that profile's worker thread and goes through
  `ankido.collection.ops`, which is the single implementation behind `/v1` and the shim. Do not
  add a second code path for the shim.
- The hard invariants in [docs/SPEC.md](docs/SPEC.md#4-hard-invariants) are not up for
  negotiation in a PR: Anki owns scheduling, one writer per collection, no implicit full upload,
  schema upgrades gated, no anonymous surface, idempotent writes, no secrets in logs, no
  telemetry. If a change needs to bend one, open an issue first.
- New `/v1` behaviour needs a section in `docs/api.md`; new config keys need a row in
  `docs/quickstart.md` and a line in `ankido.example.yaml`; user-visible changes need a
  `CHANGELOG.md` entry under `Unreleased`.
- The AnkiConnect shim gets bug fixes and missing actions, not new concepts. New features go to
  `/v1`.

## Commits and pull requests

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `perf:`, `chore:`, optionally with a scope
(`feat(queue): ...`, `fix(shim): ...`). A `!` or a `BREAKING CHANGE:` footer marks a change to
the `/v1` contract or the config format.

No CLA and no sign-off line. By opening a PR you agree that your contribution is licensed under
AGPL-3.0-or-later like the rest of the project.

Before you open the PR:

- [ ] `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`
      all pass
- [ ] tests cover the change; fixtures are synthetic
- [ ] docs and `CHANGELOG.md` updated if anything user-visible changed
- [ ] no personal data, credentials, hostnames or real cards anywhere in the diff
- [ ] one topic per PR

Small PRs get reviewed fast. If a change is large, open an issue describing it first so the shape
can be agreed before the work.

## Reporting bugs and security issues

Bugs: open an issue with the template. Include the Ankido version (`ankido --version` or the
image tag), how it is deployed, the request you made, the response, and the relevant JSON log
lines with any token ids removed.

Security issues: do not open an issue. Follow [SECURITY.md](SECURITY.md).

## Conduct

Be direct and be kind. Assume good faith, critique the work rather than the person, and keep
disagreements about code in the code review. Harassment, personal attacks, and discriminatory
remarks are not tolerated in any project space. If something happens, contact the maintainer
through GitHub; reports are handled privately.
