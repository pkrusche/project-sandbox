# Spec Delta

## Purpose

Defines how project-sandbox finds and runs tools on the host, where they run, and how host invocations of version-control tools are kept from honoring configuration a sandboxed agent can change.

## ADDED Requirements

### Requirement: Host tools are resolved to trusted absolute paths
The system SHALL resolve every host tool it runs (`jj`, `git`, the container runtime, agent CLIs, `pass`, `gh`, `security`) to an absolute path. Resolution SHALL ignore empty and relative `PATH` entries and any `PATH` directory inside the project or workspace. A tool found only through an ignored entry SHALL be treated as missing.

#### Scenario: Relative PATH entry
- **WHEN** `PATH` contains `.` and the project contains an executable named `jj`
- **THEN** the system runs the `jj` found through the remaining `PATH` entries, or reports `jj` as missing when there is none

#### Scenario: PATH directory inside the project
- **WHEN** `PATH` contains `<project>/bin`, which holds a `claude` executable
- **THEN** that executable is never run

### Requirement: Host tools run from a neutral directory
Agent CLIs used for token refresh, `pass`, `gh`, `security` and `git config --global` SHALL run with a working directory that is private, empty and outside every project and workspace. `jj` SHALL receive its repository explicitly (`-R`), and SHALL NOT depend on the caller's working directory.

#### Scenario: Token refresh in a project with hostile settings
- **WHEN** the project contains `.claude/settings.json` with an `apiKeyHelper`, `env` and hooks that would create a marker file, and a launch refreshes the host Claude token
- **THEN** `claude auth status` runs from a private empty directory, and no marker file is created

#### Scenario: Codex project config
- **WHEN** the project contains `.codex/config.toml`, and a Codex launch refreshes the host token
- **THEN** `codex login status` runs from a private empty directory

### Requirement: Host jj invocations ignore exec-capable configuration
Every host `jj` invocation SHALL pass configuration overrides that disable commit signing, fsmonitor and paging.

#### Scenario: Signing program in repository config
- **WHEN** the repository's jj config enables commit signing with a program that would write a host marker
- **THEN** setup and finalization (`workspace add`, `bookmark create`, `describe`, `bookmark set`, `workspace forget`) create no marker

### Requirement: Host jj uses the recorded repository config
The system SHALL record a repository's and workspace's jj config identifiers the first time it runs host jj operations on them, in host state outside the repository. Before later host jj operations, it SHALL compare the current identifiers with the record. On a mismatch it SHALL run no host jj command, and SHALL report which identifier changed and how to accept the change.

#### Scenario: Config pointer changed by an agent
- **WHEN** a session rewrites `.jj/repo/config-id`, and the next launch or this session's finalization runs
- **THEN** no host `jj` command runs, the workspace is left in place, and the message names `.jj/repo/config-id`

#### Scenario: First use
- **WHEN** project-sandbox runs host jj operations on a repository for the first time
- **THEN** the identifiers are recorded and the operations proceed
