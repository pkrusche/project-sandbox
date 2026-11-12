# Spec Delta

## Purpose

Defines how sessions in the full, read-only and hidden VCS access modes derive the Git and jj metadata mounts they add, so that no pointer file an agent can write chooses a mount source or target.

## ADDED Requirements

### Requirement: Branch-session metadata mounts come from host-known paths
For `--branch` sessions, every Git or jj metadata mount SHALL have a source and target computed from the main repository's path and the session workspace's path. No source or target SHALL be read from a file inside the workspace or the repository's metadata.

#### Scenario: Planted jj workspace pointer
- **WHEN** a jj `--branch` workspace's `.jj/repo` file was changed to `/usr/local/bin` and a new session for that bookmark starts
- **THEN** the launch is refused before a container starts, naming the file, and no mount targets `/usr/local/bin`

#### Scenario: Unchanged layout
- **WHEN** a jj or Git `--branch` session starts on a layout the host created and nothing changed it
- **THEN** the session gets the same metadata mounts as before this change

### Requirement: The jj Git backend must be the repository's own
A jj store's `git_target` SHALL be accepted only when it resolves to the main repository's own `.git` directory. Any other value SHALL refuse the launch before a container starts.

#### Scenario: Planted absolute git_target
- **WHEN** the main repository's `.jj/repo/store/git_target` names a host directory outside the repository, and a jj `--branch` session starts
- **THEN** the launch is refused naming `git_target`, and that directory is not mounted

#### Scenario: Default colocated repository
- **WHEN** `git_target` holds the relative path jj writes for a colocated repository
- **THEN** the session starts and mounts the repository's `.git`

### Requirement: In-place metadata mounts are limited to VCS metadata
In `read-only` and `hidden` sessions without `--branch`, a mount source reached through a pointer file SHALL be a Git directory or a jj store, and SHALL NOT be or contain a protected host location. Its target SHALL lie under `/workspace` or equal the source's host path, and SHALL NOT lie under a container system path such as `/usr`, `/etc`, `/bin`, `/lib`, `/sbin`, `/opt`, `/run`, `/proc`, `/sys`, `/dev` or `/home/agent`.

#### Scenario: Gitfile pointing at a credential directory
- **WHEN** the project's `.git` file names `~/.ssh` and a `read-only` session starts
- **THEN** the launch is refused naming the `.git` file, and nothing is mounted from `~/.ssh`

#### Scenario: Linked worktree of another repository
- **WHEN** the project is a Git worktree whose `.git` file names that repository's `.git/worktrees/<name>`
- **THEN** a `read-only` session mounts it read-only as before

#### Scenario: Target under a system path
- **WHEN** a pointer resolves to a valid jj store but would place it at a target under `/usr`
- **THEN** the launch is refused naming the target
