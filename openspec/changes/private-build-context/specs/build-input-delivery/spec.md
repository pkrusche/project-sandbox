# Spec Delta

## Purpose

Defines how image builds receive the files project-sandbox generates (Dockerfiles, entrypoint and setup scripts, CA files, ignore files) when the build context is the project or a user-supplied directory, so that no generated build input has to exist inside a project.

## ADDED Requirements

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

### Requirement: No generated build input is written into a build context
The system SHALL NOT create or change any file inside the project, a session directory or a `--docker-context` directory in order to build an image. A `.project-sandbox/` directory left in a context by an earlier version SHALL NOT be read by a build.

#### Scenario: Context is unchanged by a build
- **WHEN** a whole-project build runs
- **THEN** the context directory's contents are byte-identical before and after the build

#### Scenario: Concurrent builds with different settings
- **WHEN** two sessions of one project build at the same time with different base images or CA certificates
- **THEN** each image contains the generated files of its own session
