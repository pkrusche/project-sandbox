# Spec Delta

## Purpose

Defines how values taken from the repository or the command line are validated and encoded when they are rendered into generated Dockerfiles and their build-time shell steps, so that no such value is interpreted as Dockerfile or shell syntax.

## ADDED Requirements

### Requirement: Project paths in generated Dockerfiles are never parsed as syntax
Every project-relative path that a generated Dockerfile copies or passes to a build-time command SHALL be emitted as data. `COPY` SHALL use the JSON array form. Build-time commands SHALL receive the path as a separate argument, never as part of a shell command string. This applies to Cargo workspace members and npm workspace members.

#### Scenario: Member path with shell metacharacters
- **WHEN** a `--rust-cargo` workspace has a member directory named `crate$(id)` containing a `Cargo.toml`
- **THEN** the generated Dockerfile contains no shell command string containing `crate$(id)`, and building it creates the stub for exactly that directory without running `id`

#### Scenario: Member path with spaces and quotes
- **WHEN** a workspace member directory is named `my crate` or `it's`
- **THEN** the image build copies that member's `Cargo.toml` and fetches dependencies successfully

#### Scenario: Stubs removed after fetch
- **WHEN** the cache-warming layer creates `src/lib.rs` stubs for workspace members
- **THEN** exactly those stub directories are removed before the project source is copied, and no other path is removed

### Requirement: Unsafe project paths are refused before rendering
The system SHALL refuse a generated build when a workspace member path contains control characters or a newline, is absolute, contains `..`, begins with `-`, or resolves outside the project. The error SHALL name the member and the reason, and SHALL be raised before any generated file is written, including during `--dry-run`.

#### Scenario: Member path with a newline
- **WHEN** a Cargo workspace member directory name contains a newline
- **THEN** the command fails naming that member, and no Dockerfile is written

#### Scenario: Member resolves outside the project
- **WHEN** a Cargo workspace member pattern matches a symlinked directory whose target is outside the project
- **THEN** the command fails naming that member and stating that it escapes the project

#### Scenario: Member path starting with a hyphen
- **WHEN** a Cargo workspace member directory is named `-rf`
- **THEN** the command fails naming that member

#### Scenario: Dry-run reports the same error
- **WHEN** `--dry-run` is used with any of the invalid members above
- **THEN** it fails with the same error and writes no files

### Requirement: Image references and version tags are validated
The system SHALL accept `base_image` and `--image-tag` only as valid OCI image references (registry, name, tag and digest components). It SHALL accept `--python` and `--rust` only as image tag fragments that produce a valid reference in their generated `FROM` line. Invalid values SHALL fail during argument validation, before any file is written or container command is run.

#### Scenario: Newline in a version
- **WHEN** `--rust-cargo --rust $'1.87\nRUN id'` is given
- **THEN** the command fails with an error describing the accepted version format, and no Dockerfile is written

#### Scenario: Whitespace in a base image
- **WHEN** the base image argument is `python:3.12-slim AS x` or contains a newline
- **THEN** the command fails before rendering, stating that the value is not a valid image reference

#### Scenario: Valid references
- **WHEN** the base image is `ghcr.io/org/image:1.2@sha256:<64 hex>` or `localhost:5000/img`, and `--python 3.12.4` or `--rust 1.87` is given
- **THEN** the values are accepted and rendered unchanged

#### Scenario: Values from repository-committed launcher config
- **WHEN** a launcher passes a `base_image` or build version read from a repository file (for example `.project-sandbox-tui.toml`) that contains a newline
- **THEN** project-sandbox refuses it with the same validation error as for a command-line value
