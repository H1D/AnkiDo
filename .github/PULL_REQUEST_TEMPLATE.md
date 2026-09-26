## What

<!-- One paragraph: what changes and why. Link the issue if there is one. -->

## How

<!-- Anything a reviewer should know about the approach. Skip if the diff speaks for itself. -->

## Checklist

- [ ] `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright` pass
- [ ] tests cover the change; fixtures are synthetic (no real cards)
- [ ] new code is at least 80% covered
- [ ] docs updated (`docs/api.md` for `/v1` changes, `docs/quickstart.md` and
      `ankido.example.yaml` for config keys) and `CHANGELOG.md` has an entry under Unreleased
- [ ] no personal data, credentials, hostnames or tokens in the diff
- [ ] commit messages follow Conventional Commits
- [ ] the change respects the hard invariants in `docs/SPEC.md` section 4, or an issue explains
      why it must not

## Breaking changes

<!-- Any change to the `/v1` contract, the config format, the CLI, or the on-disk layout under
     data_dir. Write "none" if none. -->
