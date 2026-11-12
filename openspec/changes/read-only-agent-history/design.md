# Design

## Context

- The persisted history is at `$XDG_STATE_HOME/project-sandbox/mounts/projects/<hash>/history/claude_projects/` (`paths.ensure_history_paths`). Interactive sessions mount it read-write at `/home/agent/.claude/projects` (`HISTORY_CLAUDE_PROJECTS_TARGET`); headless sessions don't mount it.
- The container has no `CAP_SYS_ADMIN`; the agent process runs as the unprivileged `agent` user after the entrypoint's root steps.

## Goals / Non-Goals

**Goals:**
- A session can read earlier history and write its own, without changing what later sessions read.

**Non-Goals:**
- History of agents other than Claude. They have no persisted project history today.
- Filtering or reviewing history content.

## Decisions

### 1. A read-only persisted copy plus a writable in-session copy

- **Default mount:** `history/claude_projects` is mounted read-only at `/project-sandbox-history/claude_projects`.
- **Entrypoint seeding:** in the agent-user part of the entrypoint, before the agent starts, it copies it into `$HOME/.claude/projects` (`cp -a`; the container's own writable layer, discarded with the container).
- **`--persist-agent-history`:** mounts the directory read-write directly at `$HOME/.claude/projects`, and skips seeding.
- **Headless sessions** get the same default.
- **Size:** if the persisted history exceeds a threshold (default 256 MiB), the launch warns before copying. The history is per project, so it holds only this project's transcripts.

**Alternative considered:** an overlayfs mount in the root stage. That needs `CAP_SYS_ADMIN`, which the container does not and should not have. Rejected.

### 2. Telling the user

An interactive session without the flag prints one line at start: agent history from this session won't be kept, and `--persist-agent-history` changes that.

## Risks / Trade-offs

- [Users lose memory they expected to keep] → **BREAKING**, announced at session start and in the release notes.
- [Copying history adds launch time] → Bounded by the size warning. `--persist-agent-history` skips the copy.
- [Claude may write history elsewhere in a future version] → The e2e check in task 2.2 fails when a memory file persists without the flag.
