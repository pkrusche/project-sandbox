# Spec Delta

## Purpose

Defines what `--branch` sessions require and how their jj workspaces are leased, mounted and validated, so that no repository file a sandboxed agent can write decides what the host mounts or where.

## ADDED Requirements

### Requirement: --branch requires a jj repository
`--branch` SHALL be supported only for projects with a `.jj` directory. For any other project, the launch SHALL fail before creating anything, and SHALL print an instruction to run `jj git init --colocate` in the project. The system SHALL NOT initialize a repository itself.

#### Scenario: Git-only repository
- **WHEN** `--branch feat` is used in a project with `.git` and no `.jj`
- **THEN** the command fails naming the project and `jj git init --colocate`, and no branch, worktree or file is created

#### Scenario: Dry-run
- **WHEN** `--branch feat --dry-run` is used in a Git-only project
- **THEN** the same error is reported

#### Scenario: No --branch in a Git repository
- **WHEN** a Git-only project is launched without `--branch`
- **THEN** the session runs on the project directory as before

### Requirement: Workspace mounts are derived from the host layout
The system SHALL compute the jj store mount and the Git-backend mount from the host's repository layout. The store source is `<repo>/.jj/repo`. The Git-backend source is `<repo>/.git` for a colocated repository, and absent otherwise. Container targets SHALL come from the host's relative path between the workspace and those sources. The system SHALL NOT read mount sources or targets from the workspace's `.jj/repo` pointer or from `.jj/repo/store/git_target`.

#### Scenario: Absolute git_target planted by an agent
- **WHEN** `.jj/repo/store/git_target` contains `/Users/someone`
- **THEN** no mount uses `/Users/someone` as its source

#### Scenario: Pointer redirected to an image path
- **WHEN** the workspace's `.jj/repo` pointer names `/usr/local/bin`
- **THEN** no mount targets `/usr/local/bin`

### Requirement: Tampered workspace pointers refuse the launch
Before launch, the system SHALL compare the workspace's `.jj/repo` pointer and the store's `git_target` with the values jj writes for the host layout. On a mismatch, the launch SHALL fail before starting a container, naming the file and how to recreate the workspace.

#### Scenario: Pointer content changed
- **WHEN** a previous session changed the workspace's `.jj/repo` pointer or `store/git_target`
- **THEN** the next launch for that bookmark fails, naming the changed file

### Requirement: Workspace mount values are validated
Every workspace mount source SHALL resolve inside the repository and SHALL NOT be a symlink. Every target SHALL be absolute, SHALL contain no `..` after normalization, and SHALL end in `/.jj/repo` or `/.git`. Neither SHALL contain a comma or a control character. A violation SHALL fail the launch, naming the value.

#### Scenario: Comma in a repository path
- **WHEN** the repository's absolute path contains a comma
- **THEN** the launch fails, explaining that the path cannot be mounted safely

#### Scenario: Symlinked store
- **WHEN** `<repo>/.jj/repo` is a symlink
- **THEN** the launch fails, naming it

### Requirement: One session per workspace
A `--branch` session SHALL hold an exclusive lease on its jj workspace from setup until finalization completes. A second session for the same bookmark SHALL fail before starting a container, naming the bookmark and the running session. The same applies while a running container from an earlier session still uses the workspace.

#### Scenario: Concurrent sessions on one bookmark
- **WHEN** a session for bookmark `feat` is running, and another `--branch feat` launch starts
- **THEN** the second launch fails, naming `feat` and the running session's ID, and the first session's workspace is untouched

#### Scenario: Orphaned container
- **WHEN** an earlier session's CLI was killed with `SIGKILL` but its container is still running
- **THEN** a new `--branch` launch for that bookmark fails, naming the container

#### Scenario: Lease after a clean exit
- **WHEN** the previous session for a bookmark finished and its container is gone
- **THEN** a new launch for that bookmark acquires the lease and proceeds
