# Spec Delta

## Purpose

Defines which history from earlier sessions a sandbox can see, which history a session may persist for later sessions, and how the user opts in to persisting agent history.

## ADDED Requirements

### Requirement: Agent history is readable but not persisted by default
Every Claude session, interactive or headless, SHALL see the project's persisted Claude history (`~/.claude/projects`: transcripts and memory) as it stood when the session started. Changes the session makes to that history SHALL NOT be persisted, unless the user opts in with `--persist-agent-history`. The agent SHALL still be able to write its history during the session.

#### Scenario: Interactive session reads earlier history
- **WHEN** an interactive Claude session starts in a project with persisted history
- **THEN** earlier transcripts and memory are available under `~/.claude/projects` in the sandbox

#### Scenario: Headless session reads earlier history
- **WHEN** a headless Claude session starts in a project with persisted history
- **THEN** earlier transcripts and memory are available, which is new behavior: previously headless sessions had no history

#### Scenario: Writes during the session
- **WHEN** Claude writes a transcript or memory file during a session without `--persist-agent-history`
- **THEN** the write succeeds inside the session, and after the session ends the persisted history is byte-identical to its state before the session

#### Scenario: Persisted copy cannot be modified from the sandbox
- **WHEN** a process in the session tries to write to the mounted persisted history directly
- **THEN** the write fails with a read-only error

### Requirement: Writable agent history is an explicit opt-in
With `--persist-agent-history`, the session SHALL mount the project's persisted Claude history read-write, and changes SHALL persist for later sessions. `--dry-run` SHALL state whether agent history is persisted.

#### Scenario: Opt-in session
- **WHEN** a session runs with `--persist-agent-history` and Claude writes a memory file
- **THEN** the file is present in the persisted history after the session, and the next session sees it

#### Scenario: Dry-run reports the mode
- **WHEN** `--dry-run` is used, with or without `--persist-agent-history`
- **THEN** the output states whether agent history is read-only or persisted

### Requirement: Shell history persists for interactive sessions only
Interactive sessions SHALL persist bash history, read-write, in private host storage. Headless sessions SHALL NOT mount shell history.

#### Scenario: Interactive shell history
- **WHEN** a user runs commands in an interactive session and starts another interactive session later
- **THEN** the earlier commands are in the new session's bash history

#### Scenario: Headless session
- **WHEN** a headless session runs
- **THEN** no shell history is mounted
