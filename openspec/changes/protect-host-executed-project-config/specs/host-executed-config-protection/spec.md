# Spec Delta

## Purpose

Defines how project-sandbox keeps sandboxed agents from silently changing repository files that the user's own host tools execute later: Git hooks and configuration, and agent and IDE project config.

## ADDED Requirements

### Requirement: Git hooks and info directories are read-only in sandboxes
Every sandbox session SHALL mount the repository's `.git/hooks/` and `.git/info/` read-only at their in-container locations. This applies to the workspace's `.git` in sessions without `--branch`, and to the main repository's `.git` in colocated jj `--branch` sessions. Existing hooks SHALL remain usable inside the sandbox.

#### Scenario: Agent writes a hook
- **WHEN** a process in the session writes `.git/hooks/pre-commit`
- **THEN** the write fails, and the host's `.git/hooks/` is unchanged after the session

#### Scenario: Agent writes info/attributes
- **WHEN** a process in the session writes `.git/info/attributes`
- **THEN** the write fails

#### Scenario: Existing hooks still run in the sandbox
- **WHEN** the repository has a `pre-commit` hook and the agent runs `git commit` in a session without `--branch`
- **THEN** the hook runs inside the sandbox

#### Scenario: Missing directories
- **WHEN** `.git/hooks/` or `.git/info/` does not exist before launch
- **THEN** the host creates it empty before starting the container, and it is mounted read-only

### Requirement: Git config files are snapshotted before each session
Before the container starts, the system SHALL snapshot `.git/config` and every `.git/worktrees/*/config.worktree`. If another session in the same repository is still running, it SHALL reuse that session's snapshot, so that a snapshot never captures a running agent's edits.

#### Scenario: Concurrent sessions
- **WHEN** session A's agent changes `.git/config`, then session B starts in the same repository while A is running, and A ends before B
- **THEN** B uses the snapshot taken before A started. When B ends, `.git/config` equals that original snapshot, never A's agent's version

### Requirement: Git config files are restored after each session
After the container exits, before any other teardown step, the system SHALL compare each snapshotted Git config file with its snapshot. A differing file SHALL be moved aside without following links, to a quarantine name containing the session ID, and the snapshot SHALL be written back. A file that had no snapshot SHALL be moved aside.

#### Scenario: Agent sets core.hooksPath
- **WHEN** a session runs `git config core.hooksPath .evil`
- **THEN** after the session, `.git/config` equals its pre-session contents, the agent's version is at `.git/config.project-sandbox-<session-id>`, and the report flags `core.hooksPath` as exec-capable

#### Scenario: Agent replaces .git/config with a symlink
- **WHEN** a session replaces `.git/config` with a symlink to a file outside the repository
- **THEN** after the session, `.git/config` is a regular file with the pre-session contents, and the link's target is unchanged

#### Scenario: Host edit during a session
- **WHEN** the user runs `git push -u` on the host while a session runs, which changes `.git/config`
- **THEN** after the session, the change is reverted and kept in the quarantine file, and the report tells the user how to re-apply it

#### Scenario: Interrupted session
- **WHEN** the session is interrupted with `Ctrl-C` or `--timeout`
- **THEN** the restore still runs before the CLI exits

### Requirement: Host-executed project config paths are watched
The system SHALL watch these paths:
- `.claude/settings.json` and `.claude/settings.local.json`;
- `.mcp.json`;
- `.codex/`, `.pi/` and `.opencode/`;
- `opencode.json` and `opencode.jsonc`;
- `.vscode/tasks.json`, `.vscode/settings.json` and `.vscode/launch.json`;
- `.idea/runConfigurations/` and `.run/`;
- the pre-session `core.hooksPath` directory, when it lies inside the project.

#### Scenario: Husky hooks
- **WHEN** the project sets `core.hooksPath=.husky`, and a session modifies `.husky/pre-commit`
- **THEN** the report names `.husky/pre-commit` as modified

#### Scenario: hooksPath outside the project
- **WHEN** `core.hooksPath` points outside the project
- **THEN** that directory is not watched

### Requirement: Changes to watched paths are reported
Before and after each session, the system SHALL record a manifest of the watched paths, without following links. Each entry records its type, size and content hash. After the session, it SHALL print a report listing each watched path that was added, removed, modified or changed type. The report SHALL NOT revert or block anything.

#### Scenario: Agent adds a Claude hook
- **WHEN** a session adds a `hooks` entry to `.claude/settings.json`
- **THEN** the session-end report names `.claude/settings.json` as modified

#### Scenario: Agent adds an OpenCode plugin
- **WHEN** a session creates `.opencode/plugin/x.js`
- **THEN** the report names `.opencode/plugin/x.js` as added

#### Scenario: Watched file replaced by a symlink
- **WHEN** a session replaces `.vscode/tasks.json` with a symlink
- **THEN** the report names it as changed type, and its target is not read

#### Scenario: No changes
- **WHEN** a session changes none of the watched paths and no Git config file
- **THEN** no report is printed

### Requirement: The report reaches launchers
When `--json-summary` is given, the summary SHALL include a `config_changes` list. Each entry SHALL have a path relative to the watched tree, the change kind and, for restored Git config files, the quarantine path and the exec-capable keys. The existing summary fields SHALL be unchanged.

#### Scenario: Headless run from a launcher
- **WHEN** a headless session with `--json-summary=<file>` changed `.mcp.json` and `.git/config`
- **THEN** `<file>` contains `config_changes` with both entries, and still contains `session_id` and `exit_code` as before

### Requirement: --branch sessions protect both trees
For a `--branch` session, the system SHALL watch and report paths in the jj workspace. It SHALL apply the read-only mounts and the config restore to the main repository's `.git`.

#### Scenario: Branch session changes workspace settings and main .git config
- **WHEN** a `--branch` session modifies `<workspace>/.claude/settings.json` and the main repository's `.git/config`
- **THEN** the report lists both, and the main `.git/config` is restored
