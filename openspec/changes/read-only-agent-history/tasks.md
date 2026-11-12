# Tasks

## 1. Read-only default

- [ ] 1.1 Mount `history/claude_projects` read-only at `/project-sandbox-history/claude_projects` for interactive and headless sessions, seed `$HOME/.claude/projects` from it in `entrypoint.sh.j2` as the agent user before the agent starts, add the size warning above 256 MiB and the session-start notice. Verify with renderer tests on the seeding step, and with CLI tests on the mount mode and the notice for both session kinds.

## 2. Opt-in

- [ ] 2.1 Add `--persist-agent-history`: mount read-write at `$HOME/.claude/projects`, skip seeding, and show the mode in `--dry-run`. Verify with CLI tests with and without the flag.
- [ ] 2.2 Add an e2e check that a memory file written without the flag does not persist and the persisted history is byte-identical afterwards, and that it does persist with the flag. Verify on Docker and Apple `container`.
- [ ] 2.3 Document the default, the flag and what is lost without it in `docs/usage.md` and `docs/security.md`, and add the BREAKING note to the CHANGELOG. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
