# Spec Delta

## Purpose

Defines how project-sandbox finds and runs tools on the host, where they run, and how its host Git and jj invocations are kept from running programs that a sandboxed agent can name through repository files or configuration.

## ADDED Requirements

### Requirement: Host tools are resolved to trusted absolute paths
The system SHALL resolve every host tool it runs (`jj`, `git`, the container runtime, agent CLIs, `pass`, `gh`, `security`) to an absolute path. Resolution SHALL ignore empty and relative `PATH` entries and any `PATH` directory inside the project or a session directory. A tool found only through an ignored entry SHALL be reported as missing, naming the ignored entry.

#### Scenario: Relative PATH entry
- **WHEN** `PATH` contains `.` and the project contains an executable named `jj`
- **THEN** the system runs the `jj` found through the remaining `PATH` entries, or reports `jj` as missing when there is none

#### Scenario: PATH directory inside the project
- **WHEN** `PATH` contains `<project>/bin`, which holds a `claude` executable
- **THEN** that executable is never run

### Requirement: Host tools run from a neutral directory
Agent CLIs used for token refresh, `pass`, `gh`, `security` and `git config --global` SHALL run with a working directory that is private, empty and outside every project and session directory. Host `git` and `jj` calls SHALL name their repository explicitly and SHALL NOT depend on the caller's working directory.

#### Scenario: Token refresh in a project with hostile settings
- **WHEN** the project contains `.claude/settings.json` with an `apiKeyHelper`, `env` and hooks that would create a marker file, and a launch refreshes the host Claude token
- **THEN** `claude auth status` runs from a private empty directory, and no marker file is created

#### Scenario: Codex project config
- **WHEN** the project contains `.codex/config.toml` and a Codex launch refreshes the host token
- **THEN** `codex login status` runs from a private empty directory

### Requirement: Host Git invocations run no hooks or filesystem monitor
Every host `git` invocation SHALL disable repository hooks and the filesystem monitor, and every commit project-sandbox makes SHALL skip commit verification hooks.

#### Scenario: Planted pre-commit hook
- **WHEN** a Git `--branch` session in `full` mode writes an executable `.git/hooks/pre-commit` that would create a host marker, and the session ends with changes
- **THEN** the snapshot commit is created and no marker exists

#### Scenario: Planted hooks path or fsmonitor
- **WHEN** the repository's config sets `core.hooksPath` or `core.fsmonitor` to a directory or program that would create a host marker
- **THEN** setup and capture of a `--branch` session create no marker

### Requirement: Host jj invocations ignore exec-capable configuration
Every host `jj` invocation SHALL pass configuration overrides that disable commit signing, the filesystem monitor and paging, and that set the Git executable jj runs to the resolved host `git`.

#### Scenario: Signing program in repository config
- **WHEN** the repository's jj config enables commit signing with a program that would write a host marker
- **THEN** session setup and finalization (`workspace add`, `commit`, `describe`, `bookmark set`, `workspace forget`) create no marker

#### Scenario: Git executable in repository config
- **WHEN** the repository's jj config sets `git.executable-path` to a program that would write a host marker
- **THEN** `workspace forget` at the end of a session runs the resolved host `git` and creates no marker

### Requirement: doctor reports adoptable jj repository config
`project-sandbox doctor`, run inside a jj repository, SHALL warn when `.jj/repo/config.toml` exists and `.jj/repo/config-id` does not, and SHALL report the host jj version.

#### Scenario: Planted repository config
- **WHEN** a jj repository has `.jj/repo/config.toml` and no `.jj/repo/config-id`
- **THEN** `doctor` warns that the next jj command will adopt that file, naming its path

#### Scenario: Migrated repository
- **WHEN** a jj repository has both `.jj/repo/config.toml` and `.jj/repo/config-id`
- **THEN** `doctor` does not warn about it
