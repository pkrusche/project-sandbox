# Spec Delta

## Purpose

Defines where project-sandbox keeps the state it generates on the host, how that state is protected from the repository and from sandboxed agents, how concurrent sessions are kept apart, and how image builds receive generated inputs.

## ADDED Requirements

### Requirement: Generated state lives outside the project
The system SHALL store generated build inputs, agent configs, staged secrets, prompts, build-cache state, history, session logs, mask directories and lock files outside every project and worktree directory. The only generated files inside a project SHALL be in `.devcontainer/`.

#### Scenario: Launch leaves no generated files in the project
- **WHEN** a sandbox session runs to completion in a project without `.devcontainer` rendering
- **THEN** the project and its worktree contain no file or directory created by project-sandbox, and the root `.gitignore` is unchanged

#### Scenario: Devcontainer files are self-ignoring
- **WHEN** the devcontainer is rendered
- **THEN** its generated files are real files under `.devcontainer/`, `.devcontainer/.gitignore` contains `*`, and the root `.gitignore` is unchanged

#### Scenario: Dry-run
- **WHEN** `--dry-run` is used
- **THEN** the output names the state and session directories that would be used, and nothing is created anywhere

### Requirement: State directories are private and verified
Every state directory SHALL be created with mode `0700`. Before use, the system SHALL verify that each directory is owned by the invoking user, is not a symbolic link and is not writable by group or others. A directory failing verification SHALL be refused with an error naming it, and SHALL NOT be repaired or deleted automatically.

#### Scenario: Pre-planted symlink at the state root
- **WHEN** the project's state directory path is a symlink to another directory
- **THEN** the launch fails naming the path, and nothing is written through the link

#### Scenario: Wrong owner or mode
- **WHEN** a state directory is owned by another user or is group-writable
- **THEN** the launch fails naming the directory and the expected owner and mode

### Requirement: Writes never follow links
The system SHALL create, replace and delete generated files relative to verified directory descriptors, without following symbolic links at the final component. Secret files SHALL be created with mode `0600` on the first open. A symlink replacing a target file between check and write SHALL NOT redirect the write.

#### Scenario: Symlinked file in `.devcontainer`
- **WHEN** `.devcontainer/devcontainer.json` or `.devcontainer/Dockerfile` is a symlink to a file outside the project
- **THEN** the link is replaced by a regular file, and the link's target is unchanged

#### Scenario: Symlinked `.devcontainer` directory
- **WHEN** `.devcontainer` itself is a symlink
- **THEN** devcontainer rendering fails naming the path, and nothing is written through it

#### Scenario: Secret file permissions
- **WHEN** a session stages an API-key env file, an agent-proxy config or a Copilot token
- **THEN** the file is mode `0600` from creation, with no window in which it is more widely readable

### Requirement: Each session has its own staging
Each session SHALL render its build context, prompt, API-key env file, agent configs, proxy configs, staged credentials and Copilot token into a directory unique to that session. A container SHALL mount only its own session's staging. A session SHALL delete only its own staging when it exits. A later launch SHALL delete staging of sessions whose owning process no longer exists.

#### Scenario: Concurrent sessions with different prompts and proxies
- **WHEN** two sessions for the same project start at the same time with different prompts and different agent-proxy keys
- **THEN** each container sees only its own prompt and key

#### Scenario: One session exits while another starts
- **WHEN** session A exits while session B is starting
- **THEN** session B's staged credentials, prompt and token remain until session B exits

#### Scenario: `--no-forward-credentials` beside a forwarding session
- **WHEN** a session runs with `--no-forward-credentials` while another session of the same project forwards credentials
- **THEN** the other session's staged credentials are not removed

#### Scenario: Crashed session
- **WHEN** a session's process was killed with `SIGKILL`, and a later launch for any project runs
- **THEN** the killed session's staging is deleted, and the live sessions' staging is not

### Requirement: Builds receive generated inputs without project-relative paths
Image builds SHALL take generated files from the session's private build context, never from a path inside the project. When the build context is the project or a user-supplied `--docker-context`, the generated files SHALL be supplied separately from that context. `--docker-context` SHALL NOT need to contain any generated directory.

#### Scenario: Whole-project build on Docker or Podman
- **WHEN** `--python-uv`, `--rust-cargo`, `--node-npm` or `--dockerfile` builds on Docker or Podman
- **THEN** the build command passes the session's private build context as a named build context, and the generated Dockerfile copies sandbox files from it

#### Scenario: Whole-project build on Apple container
- **WHEN** the same flows build on Apple `container`
- **THEN** the generated files reach the image without any path inside the project, and the build succeeds

#### Scenario: Planted generated file in the project
- **WHEN** the project contains `.project-sandbox/init-firewall.sh` or `.project-sandbox/entrypoint.sh` with attacker content
- **THEN** the built image contains the generated versions, not the project's files

#### Scenario: Custom docker context
- **WHEN** `--dockerfile` is used with a `--docker-context` directory that does not contain the project's `.project-sandbox`
- **THEN** the build proceeds

### Requirement: Implicit mount sources are validated
Every mount source the system adds on its own (history, masks, staging, the egress CA directory) SHALL be a verified private directory or file under the state roots, and SHALL be validated immediately before the container command is built. A mask SHALL be an empty directory created for the session. A legacy `/workspace/.project-sandbox` SHALL still be masked when present.

#### Scenario: Symlinked legacy history directory
- **WHEN** the project contains `.project-sandbox/history/shell` as a symlink to an outside directory
- **THEN** that path is never mounted, and the session's history comes from private state

#### Scenario: Symlinked legacy mask directory
- **WHEN** the project contains `.project-sandbox/workspace-mask` as a symlink to an outside directory
- **THEN** no mount uses it as its source, and `/workspace/.project-sandbox` is masked by an empty directory created for the session

#### Scenario: Final mount list
- **WHEN** the container command for any session is built
- **THEN** every implicit mount source resolves under a state root, is not a symlink, and is owned by the invoking user

### Requirement: Devcontainer configuration is hidden from sandboxes
Every sandbox session SHALL mount an empty, read-only directory over `/workspace/.devcontainer`, whether or not the directory existed before launch. When it is absent, the system SHALL create `.devcontainer/` containing only its self-ignoring `.gitignore` before starting the container. A `.devcontainer` that is a symbolic link SHALL cause the launch to fail.

#### Scenario: Existing devcontainer configuration
- **WHEN** the workspace contains `.devcontainer/devcontainer.json` and a session starts
- **THEN** `/workspace/.devcontainer` is empty and read-only inside the sandbox, and the host files are unchanged after the session

#### Scenario: Agent tries to create a devcontainer configuration
- **WHEN** the workspace has no `.devcontainer` before launch, and a process in the session tries to create `/workspace/.devcontainer/devcontainer.json`
- **THEN** the write fails, and after the session the host's `.devcontainer/` contains only `.gitignore`

#### Scenario: Symlinked devcontainer directory
- **WHEN** `.devcontainer` in the workspace is a symbolic link
- **THEN** the launch fails before starting a container, naming the path and explaining that the link's target would be writable from the sandbox

#### Scenario: Worktree sessions
- **WHEN** a `--branch` session mounts a worktree at `/workspace`
- **THEN** the worktree's `.devcontainer` is masked in the same way

### Requirement: Launcher-facing outputs keep their locations and formats
Moving generated state SHALL NOT change the outputs that launchers such as project-sandbox-tui consume:
- session records SHALL stay in `$XDG_STATE_HOME/project-sandbox/sessions/<session-id>.json` with their existing fields;
- an explicit `--log` path SHALL be honored, with the Markdown transcript written beside it;
- `--json-summary` SHALL keep its existing fields, including `pid`, `exit_code` and `session_id`.

#### Scenario: Background run from a launcher
- **WHEN** a launcher runs a headless session with `--log=<dir>/run.log` and `--json-summary=<dir>/run.json`, where `<dir>` is outside every project
- **THEN** the log is written to `<dir>/run.log`, the transcript to `<dir>/run.md` and the summary to `<dir>/run.json`, and the summary contains `pid`, `exit_code` and `session_id`

#### Scenario: Session records
- **WHEN** a session is running
- **THEN** its record is at `$XDG_STATE_HOME/project-sandbox/sessions/<session-id>.json` with `session_id`, `container_name`, `project_path`, `workspace_path`, `agent`, `status`, `pid`, `started_at` and `runtime`, and no per-session staging directory is created inside that directory

### Requirement: Host lock files are private
Host advisory locks SHALL be kept in a private per-user lock directory under the state root. Opening a lock SHALL verify that it is a regular file owned by the invoking user. A lock file SHALL never be unlinked to recover from contention.

#### Scenario: Foreign lock file
- **WHEN** a lock path exists as a regular file owned by another user, or as a FIFO
- **THEN** the operation fails naming the lock path, and does not wait on or truncate it

### Requirement: Existing in-project state is migrated safely
On the first launch after upgrading, the system SHALL move history and session logs from `<project>/.project-sandbox/` into private state, and SHALL delete the generated files it recognizes there, without following links. It SHALL leave unrecognized files in place and report them. `--dry-run` SHALL report the planned migration without changing anything.

#### Scenario: Stale secrets removed
- **WHEN** `.project-sandbox/` contains `api-keys.env`, `pi/models.json` or `opencode/opencode.json` from an earlier version
- **THEN** those files are deleted, and the launch reports that they were removed

#### Scenario: Symlink during migration
- **WHEN** `.project-sandbox/history` or a generated file in `.project-sandbox/` is a symlink
- **THEN** the link itself is removed or skipped, and its target is neither read, moved nor deleted

#### Scenario: Unknown files kept
- **WHEN** `.project-sandbox/` contains a file the system did not generate
- **THEN** that file is kept, and the launch names it in a notice
