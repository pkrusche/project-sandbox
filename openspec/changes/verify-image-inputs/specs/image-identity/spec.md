# Spec Delta

## Purpose

Defines how sandbox images record the inputs and settings they were built from, when an existing image may be reused, and what `--no-build` guarantees about the image a session runs.

## ADDED Requirements

### Requirement: Images are labeled with their inputs and settings
Every image the system builds SHALL carry a label with a fingerprint over every generated build input and the image-affecting settings, and a label with a secret-free summary of those settings.

#### Scenario: Labels on a fresh build
- **WHEN** a session builds its image
- **THEN** `image inspect` on the tag shows both labels, and the fingerprint equals the one the session computed

#### Scenario: Secret-free settings
- **WHEN** the build uses a build proxy with credentials in its URL
- **THEN** the settings label contains the proxy endpoint without the credentials

#### Scenario: Every generated file counts
- **WHEN** only a generated file outside the previously fingerprinted names changes
- **THEN** the fingerprint changes and the image is rebuilt

### Requirement: An image is reused only when its label matches
The system SHALL skip a build only when the tag exists and its inputs label equals the session's fingerprint.

#### Scenario: Unchanged inputs
- **WHEN** a second session renders the same inputs for the same tag
- **THEN** no build runs

#### Scenario: Image replaced out of band
- **WHEN** the tag was rebuilt by hand from another Dockerfile
- **THEN** the next session rebuilds it

### Requirement: --no-build verifies the image against the session's inputs
With `--no-build`, the system SHALL refuse to start a container when the image's inputs label differs from the session's fingerprint or is missing, and SHALL name each settings key that differs.

#### Scenario: Image built from another base image
- **WHEN** the image was built for `python:3.12-slim` and the session asks for `python:3.13-slim` with `--no-build`
- **THEN** the launch fails before starting a container, naming `base_image` with both values

#### Scenario: Image from an older version
- **WHEN** the image has no labels
- **THEN** the launch fails with a rebuild instruction

#### Scenario: Matching image
- **WHEN** the image was built from the same inputs
- **THEN** the session starts without building

### Requirement: --build-only reports options that do not affect the image
`--build-only` SHALL warn about each given option that has no effect on the image.

#### Scenario: Option without effect
- **WHEN** `--build-only` is given an option that changes no build input
- **THEN** the image is built and the CLI prints that the option had no effect on it
