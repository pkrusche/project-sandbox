# Tasks

## 1. Checks

- [ ] 1.1 Add `mount_validation.check` with the protected roots and credential list (design Decisions 1–2), and call it for every user mount in every egress mode. Verify with CLI tests: sidecar state subdirectory in firewall mode, the policy directory, `$HOME`, `~/.ssh`, `~/.aws/sso`, `~/.claude`, each refused before any container command; an ordinary project subdirectory accepted.
- [ ] 1.2 Extend `validate_target` with the protected prefixes and `/workspace` (Decision 3), and pin mask ordering. Verify with tests for `/usr/local/bin`, `/etc/x`, `/run/x`, `/project-sandbox-config`, `/workspace`, an accepted `/opt/data`, and an argv test that the `.devcontainer` mask still follows a user mount on that path.
- [ ] 1.3 Add workspace containment (Decision 4). Verify with a test where `<project>/data` links to `$HOME/.ssh`.
- [ ] 1.4 Run the same checks in `devcontainer.render`. Verify with a renderer test that a refused mount writes no file.

## 2. Docs

- [ ] 2.1 Document the rules in `docs/usage.md` (`--mount`) and `docs/security.md`, including the BREAKING note for the CHANGELOG. Verify that `uv run pytest -q`, `uv run ruff check src scripts tests` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- Archive with `/opsx:archive`, creating `openspec/specs/mount-validation/`.
