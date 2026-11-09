# Proposal

## Why

Claude's `~/.claude/projects` history holds session transcripts and auto-memory. Interactive sessions mount the project's persisted copy read-write, so an agent can change what later sessions read as their own memory and past conversations. A prompt injection in one session can therefore steer every later one. Headless sessions get no history at all.

The user decided (2026-11-09) that this history is read-only for interactive and headless sessions alike, with an explicit opt-in to persist it. This change was split from `private-session-state` on 2026-11-09, because it changes behavior independently of where the history is stored.

## What Changes

- **Agent history is read-only by default.**
  - The project's persisted Claude history is mounted read-only into both interactive and headless sessions.
  - It is copied into a writable scratch location in the container, so the agent can still write transcripts and memory during the session. Those writes are discarded with the container.
  - **BREAKING:** interactive sessions no longer persist transcripts or memory by default. Headless sessions now see earlier history.
- **`--persist-agent-history`** opts in to the old read-write behavior. `--dry-run` states the mode.
- **Shell history is unchanged:** interactive only, read-write.

## Capabilities

### New Capabilities

- `session-history`: which history a sandbox sees, whether it is persisted, and the opt-in for writable agent history.

### Modified Capabilities

(none)

## Impact

- **Code:** `cli.py` (mounts, the flag, dry-run), `templates/entrypoint.sh.j2` (seeding the writable copy), `paths.py` (history paths).
- **Depends on:** `private-session-state`, which moves the history into private state and validates it as a mount source.
- **Users:** memory written in a session is lost unless the flag is given. The release notes and the session start output say so.
- **Docs:** `docs/usage.md` (history, the flag), `docs/security.md`.
