# Spec Delta

## Purpose

Defines the opt-in isolated VCS mode: an agent works in a session directory with a private repository, never sees or writes the main repository's Git or jj metadata, and the host records the result as one commit using only metadata it owns.

## ADDED Requirements

### Requirement: Isolated mode is an opt-in VCS access mode
The CLI SHALL accept `--vcs-access isolated` in addition to `full`, `read-only` and `hidden`, and the default SHALL remain `full`. Isolated mode SHALL use the jj backend when the project has a `.jj` directory and the Git backend when it has `.git` and no `.jj`.

#### Scenario: Default is unchanged
- **WHEN** a session is launched without `--vcs-access`
- **THEN** it runs in `full` mode exactly as before this change

#### Scenario: Git-only project
- **WHEN** `--vcs-access isolated` is used in a project with `.git` and no `.jj`
- **THEN** the session runs isolated with the Git backend

#### Scenario: jj project
- **WHEN** `--vcs-access isolated` is used in a project with `.jj`
- **THEN** the session runs isolated with the jj backend

### Requirement: Unsupported isolated launches are refused before anything is created
A launch with `--vcs-access isolated` SHALL fail before creating any directory, branch, bookmark or container when the project has neither `.git` nor `.jj`, or when the selected runtime cannot provide the session's mounts.

#### Scenario: No repository
- **WHEN** `--vcs-access isolated` is used in a directory with neither `.git` nor `.jj`
- **THEN** the command fails naming the missing repository, and nothing is created

#### Scenario: Unsupported runtime
- **WHEN** `--vcs-access isolated` is used with the chroot runtime
- **THEN** the command fails naming the runtime, and nothing is created

### Requirement: Dry-run describes an isolated session
`--dry-run` with `--vcs-access isolated` SHALL report the mode, the backend, the branch or bookmark and the session directory, and SHALL create nothing.

#### Scenario: Dry-run
- **WHEN** `--vcs-access isolated --dry-run` is used
- **THEN** the output names the mode, the backend, the branch or bookmark and the session directory, and no directory, branch, bookmark or container is created

### Requirement: An isolated session never mounts the main repository's metadata
In isolated mode the container SHALL mount a session directory at `/workspace`. No mount source SHALL lie inside the main repository's `.git` or `.jj`, and no mount source or target SHALL be derived from a file inside the session directory. The host's own metadata for the session directory SHALL be neither readable nor writable from the container.

#### Scenario: Mounts of an isolated session
- **WHEN** the container command for an isolated session is built, for a Git project, a non-colocated jj project and a colocated jj project
- **THEN** no mount source lies inside the main repository's `.git` or `.jj`

#### Scenario: Planted pointer in a kept session
- **WHEN** a kept isolated session directory contains a file at `.jj/repo` or `.git` written by an earlier agent, and a new session resumes it
- **THEN** the new session's mounts are the same as for a fresh session, and no mount uses a path read from that file

#### Scenario: Agent looks for the host's metadata
- **WHEN** a process in an isolated session reads, writes, renames or removes `/workspace/.git` or `/workspace/.jj`
- **THEN** it reaches only the session's private repository or fails, and the host's metadata for the session directory is unchanged

### Requirement: An isolated session has a private repository
In isolated mode `/workspace` SHALL contain a working repository private to the session: Git for the Git backend, and colocated jj and Git for the jj backend. When a new session starts, the repository SHALL hold one base commit with the session's starting files and a clean working copy. Nothing the agent does in that repository SHALL reach the main repository.

#### Scenario: Agent inspects its changes
- **WHEN** the agent edits a file and runs `git diff` (or `jj diff` with the jj backend)
- **THEN** the output shows that edit against the session's starting files

#### Scenario: Agent commits
- **WHEN** the agent creates commits in an isolated session
- **THEN** the commands succeed, and none of those commits or their messages appear in the main repository after the session

#### Scenario: Agent plants a hook or config
- **WHEN** the agent writes `/workspace/.git/hooks/pre-commit`, changes `/workspace/.git/config`, or writes jj repository config
- **THEN** the main repository's hooks and config are unchanged, and no host command reads the planted files

### Requirement: The host records an isolated session's result as one commit
When an isolated session ends, including after a killed container or a timeout, the host SHALL record the session directory's files as one commit on the session's branch or bookmark, using only metadata the host created. A session that changed nothing SHALL leave the branch or bookmark at its starting commit. Symbolic links SHALL be recorded as links without reading their targets.

#### Scenario: Changes are captured
- **WHEN** an isolated session modified, added and deleted files and then ends
- **THEN** the session's branch or bookmark points at one new commit containing exactly those changes

#### Scenario: Container killed
- **WHEN** the container of an isolated session is killed or times out
- **THEN** the host still records the files as they were left

#### Scenario: Symbolic link created by the agent
- **WHEN** the agent creates a symbolic link to a file outside the session directory
- **THEN** the commit records a link, and the target's content is not read

#### Scenario: No changes
- **WHEN** an isolated session ends without changing any file
- **THEN** no new commit is created

### Requirement: Host commands of an isolated session run no repository-controlled programs
Host Git and jj commands for an isolated session's setup and capture SHALL NOT run repository hooks or a filesystem monitor, and SHALL NOT read configuration from the session directory or the private repository.

#### Scenario: Hooks are not run
- **WHEN** the main repository has a `pre-commit` hook and an isolated session with changes ends
- **THEN** the result commit is created without running the hook

#### Scenario: Filesystem monitor is not run
- **WHEN** the main repository's config sets `core.fsmonitor` to a program and an isolated session ends
- **THEN** that program is not run during setup or capture

### Requirement: Unsafe results are kept for review instead of captured
The host SHALL NOT capture automatically, and SHALL keep the session directory and print its path with instructions, when the session directory contains a nested `.git` or `.jj` entry below its root, or when the session's metadata layout is not the one the host created.

#### Scenario: Nested repository
- **WHEN** the agent created `/workspace/sub/.git`
- **THEN** no commit is made, the session directory is kept, and the output names `sub/.git`

#### Scenario: Layout changed
- **WHEN** the host's metadata for the session directory is missing or not the one the host created when the session ends
- **THEN** no commit is made, and the output names the session directory and how to recover it

### Requirement: The session's branch or bookmark is predictable
With `--branch NAME` the session SHALL use branch or bookmark `NAME`, continuing it if it exists. Without `--branch` it SHALL use a name generated from the session ID under `sandbox/`, which the CLI prints at the start and the end. A session for a new branch or bookmark SHALL start where `--branch` sessions start today. The JSON summary SHALL report the session directory, the branch or bookmark and the VCS access mode.

#### Scenario: Generated name
- **WHEN** an isolated session is launched without `--branch`
- **THEN** its result is on `sandbox/<session-id>`, and the CLI prints that name at the end

#### Scenario: Continuing a branch
- **WHEN** `--vcs-access isolated --branch feat` is used and `feat` exists
- **THEN** the session starts from `feat`, and `feat` points at the new result afterwards

### Requirement: One isolated session per branch or bookmark
An isolated session SHALL hold an exclusive lease on its branch or bookmark and session directory from setup until capture completes. A second session for the same branch or bookmark SHALL fail before starting a container.

#### Scenario: Concurrent sessions on one branch
- **WHEN** an isolated session for `feat` is running and another launch for `feat` starts
- **THEN** the second launch fails, naming `feat`, and the first session is unaffected

#### Scenario: Different branches
- **WHEN** two isolated sessions for different branches run at the same time
- **THEN** both run, and each result contains only its own changes

### Requirement: Kept and failed sessions can be resumed
When an isolated session fails, or `--keep-workspace` is given, the system SHALL record the result and keep the session directory and its private repository. A later isolated session for the same branch or bookmark SHALL reuse both. A session directory without its private repository, or a private repository without its session directory, SHALL be refused with the paths and the cleanup commands.

#### Scenario: Session fails
- **WHEN** an isolated session exits non-zero
- **THEN** its changes are recorded, the session directory is kept, and the CLI prints its path

#### Scenario: Resume
- **WHEN** a kept isolated session's branch is used again with `--vcs-access isolated`
- **THEN** the agent sees its earlier private repository and files, and the result is recorded on the same branch

#### Scenario: Mismatched leftovers
- **WHEN** the session directory exists but its private repository does not
- **THEN** the launch fails naming both paths and how to remove the leftover, and nothing is mounted

#### Scenario: Interrupted host process
- **WHEN** the CLI was killed during an isolated Git session and the same branch is launched again
- **THEN** the session directory's host metadata is restored before setup continues, and the earlier files are part of the next result
