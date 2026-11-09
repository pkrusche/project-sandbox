# Spec Delta

## Purpose

Defines the version-control modes a sandbox session can run in: what each mode mounts, how an isolated session's changes reach the main repository, and the limits on sessions that share the repository's real metadata.

## ADDED Requirements

### Requirement: The user chooses the session's VCS mode
The CLI SHALL accept `--vcs isolated` and `--vcs shared`. In a project with a `.jj` directory, the default SHALL be `isolated`. `--dry-run` SHALL show the mode, the bookmark and the session directory. `--branch` SHALL be rejected together with `--vcs shared`.

#### Scenario: Default in a jj project
- **WHEN** a session is launched in a jj project without `--vcs`
- **THEN** it runs in isolated mode

#### Scenario: Explicit shared mode
- **WHEN** a session is launched with `--vcs shared`
- **THEN** the project directory is mounted in place at `/workspace`, with its `.git` and `.jj` writable

#### Scenario: Branch with shared mode
- **WHEN** `--vcs shared --branch feat` is given
- **THEN** the command fails during argument validation

#### Scenario: Dry-run
- **WHEN** `--dry-run` is used
- **THEN** the output names the mode, the bookmark and the session directory, and nothing is created

### Requirement: Isolated mode requires a jj repository
Isolated mode SHALL be available only for projects with a `.jj` directory. In a project with `.git` and no `.jj`, a launch without `--vcs shared` SHALL fail before creating anything, naming `jj git init --colocate` and `--vcs shared`. The system SHALL NOT initialize a repository itself. A directory with neither SHALL be mounted in place, and `--vcs` and `--branch` SHALL be rejected for it.

#### Scenario: Git-only project, default launch
- **WHEN** a launch without `--vcs` runs in a project with `.git` and no `.jj`
- **THEN** the command fails, naming `jj git init --colocate` and `--vcs shared`, and nothing is created

#### Scenario: Git-only project, shared mode
- **WHEN** `--vcs shared` is used in a Git-only project
- **THEN** the session runs on the project directory

#### Scenario: Directory without a repository
- **WHEN** a launch runs in a directory with neither `.git` nor `.jj`
- **THEN** the directory is mounted in place

### Requirement: An isolated session never sees the main repository's metadata
In isolated mode, the container SHALL mount a separate session directory at `/workspace`. It SHALL NOT mount the main repository's `.jj` or `.git`, in whole or in part. The host's own metadata for the session directory SHALL NOT be readable or writable from the container.

#### Scenario: Mounts of an isolated session
- **WHEN** the container command for an isolated session is built
- **THEN** no mount source lies inside the main repository's `.jj` or `.git`

#### Scenario: Agent looks for the host's metadata
- **WHEN** a process in an isolated session reads or writes `/workspace/.jj` or `/workspace/.git`
- **THEN** it reaches only the session's private repository, and the host's metadata for the session directory is unchanged

### Requirement: An isolated session has a private repository
In isolated mode, `/workspace` SHALL contain a working `jj` and `git` repository that is private to the session. When the session starts, it SHALL hold one base commit with the session's starting files and a clean working copy. Nothing the agent does in that repository SHALL reach the main repository.

#### Scenario: Agent inspects its changes
- **WHEN** the agent edits a file and runs `jj diff` or `git diff`
- **THEN** the output shows that edit against the session's starting files

#### Scenario: Agent commits
- **WHEN** the agent creates commits in an isolated session
- **THEN** the commands succeed, and none of those commits or their messages appear in the main repository after the session

#### Scenario: Agent plants a hook
- **WHEN** the agent writes `/workspace/.git/hooks/pre-commit` or changes `/workspace/.git/config`
- **THEN** the main repository's `.git/hooks` and `.git/config` are unchanged, and no host command reads the planted files

### Requirement: Ignored folders can be shared into an isolated session
`--share PATH[:rw]` SHALL mount the project's directory `PATH` at `/workspace/PATH` in an isolated session, read-only unless `:rw` is given. Changes made through a writable share SHALL NOT be part of the session's result commit. The launch SHALL list every writable share. In shared mode the option SHALL be accepted without effect.

#### Scenario: Read-only data folder
- **WHEN** `--share data` is given, and `data/` is an ignored directory in the project
- **THEN** the agent can read `/workspace/data`, writes to it fail, and the result commit contains nothing under `data/`

#### Scenario: Writable share
- **WHEN** `--share out:rw` is given and the agent writes `/workspace/out/report.txt`
- **THEN** the file appears in the project's `out/` directory, the launch output named `out` as writable, and the result commit doesn't contain it

#### Scenario: Agent's own tools ignore the share
- **WHEN** a folder is shared and the agent runs `jj status` or `git status`
- **THEN** the shared folder's files are not reported as new

### Requirement: Only safe paths can be shared
A `--share` path SHALL be a project-relative directory that exists, resolves inside the project, and is absent from the session's checkout. The launch SHALL fail before starting a container for any other path, including a single file, a tracked path, repository metadata, `.devcontainer`, and host-executed config paths.

#### Scenario: Tracked path
- **WHEN** `--share src` is given and `src/` holds tracked files
- **THEN** the launch fails, explaining that tracked paths are already in the session, and no session directory is left behind

#### Scenario: Repository metadata
- **WHEN** `--share .git` or `--share .jj/repo` is given
- **THEN** the launch fails, naming the path

#### Scenario: Symlink leaving the project
- **WHEN** `--share data` is given and `data` is a symlink to a directory outside the project
- **THEN** the launch fails, stating that the path resolves outside the project

#### Scenario: Single file
- **WHEN** `--share .env` is given
- **THEN** the launch fails, stating that only directories can be shared

### Requirement: The host records an isolated session's result as one commit
When an isolated session ends, the host SHALL record the session directory's files as one commit in the main repository and point the session's bookmark at it. It SHALL do so without reading version-control metadata the agent could write. A session that changed nothing SHALL leave the bookmark at its starting commit.

#### Scenario: Changes are captured
- **WHEN** an isolated session modified, added and deleted files and then ends
- **THEN** the session's bookmark in the main repository points at one commit containing exactly those changes

#### Scenario: Container killed
- **WHEN** the container of an isolated session is killed or times out
- **THEN** the host still records the files as they were left

#### Scenario: Symbolic link created by the agent
- **WHEN** the agent creates a symbolic link to a file outside the session directory
- **THEN** the commit records a link, and the target's content is not read

#### Scenario: No changes
- **WHEN** an isolated session ends without changing any file
- **THEN** no new non-empty commit is created

### Requirement: The session's bookmark and start are predictable
With `--branch NAME`, the bookmark SHALL be `NAME`. Without it, the bookmark SHALL be generated from the session ID. A session for a new bookmark SHALL start from the main repository's working-copy state. A session for an existing bookmark SHALL start from that bookmark. The JSON summary SHALL keep its `workspace_path`, `bookmark` and `jj_change_id` fields.

#### Scenario: Generated bookmark
- **WHEN** an isolated session is launched without `--branch`
- **THEN** its result is on a bookmark derived from the session ID, and the CLI prints that bookmark at the end

#### Scenario: Continuing a bookmark
- **WHEN** `--branch feat` is used and bookmark `feat` exists
- **THEN** the session starts from `feat`, and `feat` points at the new result afterwards

### Requirement: One isolated session per bookmark
An isolated session SHALL hold an exclusive lease on its bookmark's session directory from setup until finalization completes. A second session for the same bookmark SHALL fail before starting a container, naming the bookmark and the running session.

#### Scenario: Concurrent sessions on one bookmark
- **WHEN** a session for bookmark `feat` is running, and another `--branch feat` launch starts
- **THEN** the second launch fails, naming `feat` and the running session's ID

#### Scenario: Different bookmarks
- **WHEN** two isolated sessions for different bookmarks run at the same time
- **THEN** both run, and each result contains only its own changes

### Requirement: Failed and kept sessions can be resumed
When a session fails, or `--keep-workspace` is given, the system SHALL keep the session directory and the session's private repository, after recording the result. A later session for the same bookmark SHALL reuse both.

#### Scenario: Session fails
- **WHEN** an isolated session exits non-zero
- **THEN** its changes are recorded, the session directory is kept, and the CLI prints its path

#### Scenario: Resume
- **WHEN** a new session starts for a bookmark whose session directory was kept
- **THEN** the agent sees the files and its private repository as they were left

### Requirement: Shared sessions are exclusive and announced
At most one `--vcs shared` session SHALL run per repository at a time. A second SHALL fail before starting a container, naming the running session. Isolated sessions SHALL be allowed beside a shared session. Every shared launch SHALL print that the agent can change repository metadata which host tools execute later.

#### Scenario: Second shared session
- **WHEN** a shared session is running, and another `--vcs shared` launch starts for the same repository
- **THEN** the second launch fails, naming the running session

#### Scenario: Isolated beside shared
- **WHEN** a shared session is running, and an isolated session starts for the same repository
- **THEN** the isolated session runs

#### Scenario: Warning
- **WHEN** a shared session starts
- **THEN** the CLI prints the shared-metadata warning before the container starts
