# Spec Delta

## Purpose

Defines how project-sandbox finds and runs tools on the host, where they run, and how host invocations of version-control tools are kept from honoring configuration a sandboxed agent can change.

## ADDED Requirements

### Requirement: Host tools are resolved to trusted absolute paths
The system SHALL resolve every host tool it runs (`jj`, `git`, the container runtime, agent CLIs, `pass`, `gh`, `security`) to an absolute path. Resolution SHALL ignore empty and relative `PATH` entries and any `PATH` directory inside the project or a session directory. A tool found only through an ignored entry SHALL be treated as missing.

#### Scenario: Relative PATH entry
- **WHEN** `PATH` contains `.` and the project contains an executable named `jj`
- **THEN** the system runs the `jj` found through the remaining `PATH` entries, or reports `jj` as missing when there is none

#### Scenario: PATH directory inside the project
- **WHEN** `PATH` contains `<project>/bin`, which holds a `claude` executable
- **THEN** that executable is never run

### Requirement: Host tools run from a neutral directory
Agent CLIs used for token refresh, `pass`, `gh`, `security` and `git config --global` SHALL run with a working directory that is private, empty and outside every project and session directory. `jj` SHALL receive its repository explicitly (`-R`), and SHALL NOT depend on the caller's working directory.

#### Scenario: Token refresh in a project with hostile settings
- **WHEN** the project contains `.claude/settings.json` with an `apiKeyHelper`, `env` and hooks that would create a marker file, and a launch refreshes the host Claude token
- **THEN** `claude auth status` runs from a private empty directory, and no marker file is created

#### Scenario: Codex project config
- **WHEN** the project contains `.codex/config.toml`, and a Codex launch refreshes the host token
- **THEN** `codex login status` runs from a private empty directory

### Requirement: Host jj invocations ignore exec-capable configuration
Every host `jj` invocation SHALL pass configuration overrides that disable commit signing, fsmonitor and paging, and that set the Git executable jj runs to the resolved host `git`.

#### Scenario: Signing program in repository config
- **WHEN** the repository's jj config enables commit signing with a program that would write a host marker
- **THEN** session setup and finalization (`workspace add`, `commit`, `describe`, `bookmark set`, `workspace forget`) create no marker

#### Scenario: Git executable in repository config
- **WHEN** the repository's jj config sets `git.executable-path` to a program that would write a host marker
- **THEN** `workspace forget` at the end of a session runs the resolved host `git`, and creates no marker
