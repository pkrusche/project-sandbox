# Spec Delta

## Purpose

Defines how project-sandbox keeps sandboxed agents from silently changing repository files that the user's own host tools execute later: Git hooks and configuration, jj config pointers, and agent and IDE project config.

## ADDED Requirements

### Requirement: Git hooks and info directories are read-only in shared sessions
Every shared session SHALL mount the repository's `.git/hooks/` and `.git/info/` read-only at their in-container locations. Existing hooks SHALL remain usable inside the sandbox. A missing directory SHALL be created empty on the host before the container starts.

#### Scenario: Agent writes a hook
- **WHEN** a process in a shared session writes `.git/hooks/pre-commit`
- **THEN** the write fails, and the host's `.git/hooks/` is unchanged after the session

#### Scenario: Agent writes info/attributes
- **WHEN** a process in a shared session writes `.git/info/attributes`
- **THEN** the write fails

#### Scenario: Existing hooks still run in the sandbox
- **WHEN** the repository has a `pre-commit` hook and the agent runs `git commit` in a shared session
- **THEN** the hook runs inside the sandbox

#### Scenario: Isolated session
- **WHEN** an isolated session starts
- **THEN** no mount refers to the main repository's `.git/hooks/` or `.git/info/`

### Requirement: Repository config files cannot be changed during a shared session
On platforms where the mechanism is available, a shared session SHALL NOT be able to modify, replace or delete `.git/config`, any `.git/worktrees/*/config.worktree`, `.jj/repo/config-id` or `.jj/workspace-config-id`. On other platforms, the launch SHALL state that these files are only restored afterwards. Any host-side lock the system places on these files SHALL be removed when the session ends, and by the next launch if the session was killed.

#### Scenario: Agent sets core.sshCommand
- **WHEN** a process in a shared session runs `git config core.sshCommand evil` on a supported platform
- **THEN** the command fails, and the host's `.git/config` is unchanged while the session is still running

#### Scenario: Agent replaces the file
- **WHEN** a process in a shared session tries to rename another file over `.git/config` or `.jj/repo/config-id` on a supported platform
- **THEN** the rename fails

#### Scenario: Killed session
- **WHEN** the CLI of a shared session was killed, and a later launch for the repository runs
- **THEN** the files are writable on the host again

#### Scenario: Unsupported platform
- **WHEN** a shared session starts on a platform without the mechanism
- **THEN** the launch prints that repository config is restored after the session, not locked during it

### Requirement: Repository config files are restored after each shared session
Before a shared session's container starts, the system SHALL snapshot the files named in the previous requirement. After the container exits, before any other teardown step, a file that differs from its snapshot SHALL be moved aside without following links, to a name containing the session ID, and the snapshot SHALL be written back. A file that had no snapshot, including a new `.jj/repo/config.toml`, SHALL be moved aside.

#### Scenario: Agent's change survives to the end
- **WHEN** a shared session on a restore-only platform runs `git config core.hooksPath .evil`
- **THEN** after the session, `.git/config` equals its pre-session contents, the agent's version is at `.git/config.project-sandbox-<session-id>`, and the report flags `core.hooksPath` as exec-capable

#### Scenario: File replaced by a symlink
- **WHEN** a session replaces `.git/config` with a symlink to a file outside the repository
- **THEN** after the session, `.git/config` is a regular file with the pre-session contents, and the link's target is unchanged

#### Scenario: jj config pointer changed
- **WHEN** a session rewrites `.jj/repo/config-id`
- **THEN** after the session, the file has its pre-session contents, and the report names it

#### Scenario: Legacy jj config planted
- **WHEN** a session creates `.jj/repo/config.toml`
- **THEN** after the session, the file has been moved aside, and the report names it

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

### Requirement: Changes are reported while a shared session runs
During a shared session, the system SHALL check the protected files and the watched paths at a fixed short interval, without following links. It SHALL record each change it sees in the session log and in the session record, with the path, the kind of change and the time. It SHALL NOT revert or block anything on the watched paths.

#### Scenario: Launcher sees a change before the session ends
- **WHEN** a shared session's agent modifies `.claude/settings.json` and keeps running
- **THEN** within a few seconds the session record lists that path as modified

#### Scenario: Symlink planted at a watched path
- **WHEN** an agent replaces `.vscode/tasks.json` with a symlink
- **THEN** the record names it as changed type, and its target is not read

### Requirement: Changes to watched paths are reported after every session
Before and after each session, in both modes, the system SHALL record a manifest of the watched paths, without following links. After the session it SHALL print a report listing each watched path that was added, removed, modified or changed type. In isolated mode the comparison SHALL cover the session directory. The report SHALL NOT revert or block anything.

#### Scenario: Agent adds a Claude hook
- **WHEN** a session adds a `hooks` entry to `.claude/settings.json`
- **THEN** the session-end report names `.claude/settings.json` as modified

#### Scenario: Agent adds an OpenCode plugin in an isolated session
- **WHEN** an isolated session creates `.opencode/plugin/x.js`
- **THEN** the report names `.opencode/plugin/x.js` as added

#### Scenario: No changes
- **WHEN** a session changes none of the watched paths and no protected file
- **THEN** no report is printed

### Requirement: The report reaches launchers
When `--json-summary` is given, the summary SHALL include a `config_changes` list. Each entry SHALL have a path relative to its tree, the change kind and, for restored files, the quarantine path and any exec-capable Git keys. The existing summary and session-record fields SHALL be unchanged.

#### Scenario: Headless run from a launcher
- **WHEN** a headless shared session with `--json-summary=<file>` changed `.mcp.json` and `.git/config`
- **THEN** `<file>` contains `config_changes` with both entries, and still contains `session_id` and `exit_code` as before
