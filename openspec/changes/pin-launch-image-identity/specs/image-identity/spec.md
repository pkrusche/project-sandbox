# Spec Delta

## Purpose

Defines how sandbox images record the inputs and settings they were built for, when an existing image may be reused, how a session pins the exact image it launches, and what `--no-build` guarantees.

## ADDED Requirements

### Requirement: Images are labeled with their inputs and settings
Every image the system builds SHALL carry an `org.project-sandbox.inputs` label and an `org.project-sandbox.settings` label. `inputs` is a fingerprint over every file in the session's generated build context plus the settings that affect the image. `settings` is a JSON summary of those settings. The `settings` label SHALL NOT contain secrets, such as API keys, proxy credentials or tokens.

#### Scenario: Labels on a fresh build
- **WHEN** a session builds its image
- **THEN** the image's `inputs` label equals the session's fingerprint, and its `settings` label lists the base image, flow, installed agents, CA certificate hashes, build-time proxy endpoint and privilege model, and no network policy

#### Scenario: Secret-free settings
- **WHEN** a session builds through a proxy with credentials
- **THEN** neither appears in any image label

#### Scenario: Every generated file counts
- **WHEN** only a generated CA certificate file differs between two sessions
- **THEN** their fingerprints differ

### Requirement: An image is reused only when its label matches
The system SHALL skip a build only when the image for the tag exists and its `inputs` label equals the session's fingerprint. It SHALL NOT rely on state stored outside the image. Builds from whole-project contexts SHALL run every time, as before, and still carry labels.

#### Scenario: Unchanged inputs
- **WHEN** a second session renders identical inputs
- **THEN** the build is skipped, and the session reuses the image

#### Scenario: Image replaced out of band
- **WHEN** the tag was rebuilt by another tool, so that its label is missing or different
- **THEN** the session rebuilds

### Requirement: Each session launches an image no other launch can move
Builds and tag changes for one image repository on one runtime SHALL be serialized by one lock, whichever project they come from. After building or reusing its image, while still holding that lock, a session SHALL add a tag unique to itself. It SHALL start its container from that tag. It SHALL remove the tag when the session ends. A later launch SHALL remove session tags whose sessions no longer run.

#### Scenario: Two projects sharing an image tag
- **WHEN** launches from two projects use the same `--image-tag` at the same time with different inputs
- **THEN** their builds don't overlap, and each session's container runs the image built from its own inputs

#### Scenario: Concurrent launch with different inputs
- **WHEN** session A releases the build lock, and session B rebuilds the project's tag with a different base image before A's container starts
- **THEN** A's container runs the image A built or verified, and B's runs its own

#### Scenario: Teardown
- **WHEN** a session ends normally, fails or is interrupted
- **THEN** its session tag is removed, and the image is kept while other tags or containers use it

#### Scenario: Killed session
- **WHEN** a session's CLI was killed, leaving its session tag behind
- **THEN** the next launch for any project removes that tag

### Requirement: --no-build verifies the image against the session's inputs
With `--no-build`, the system SHALL render the session's inputs, and compare its fingerprint with the image's `inputs` label before starting a container. On a mismatch or a missing label, the launch SHALL fail and name each differing setting from the `settings` label.

#### Scenario: Image built from another base image
- **WHEN** an image was built with one base image, and `--no-build` is launched with another
- **THEN** the launch fails before starting a container, naming `base_image` with both values

#### Scenario: Image from an older version
- **WHEN** `--no-build` names an image with no project-sandbox labels
- **THEN** the launch fails with an instruction to rebuild

#### Scenario: Matching image
- **WHEN** `--build-only` was run, followed by a `--no-build` launch with the same build options
- **THEN** the session starts without building

### Requirement: --build-only reports options that do not affect the image
`--build-only` SHALL build and label the image a later session with the same build options would use, without adding a session tag. It SHALL warn about each given option that has no effect on the image, such as `--pi-ollama`, `--extra-domain`, `--agent-proxy` and `--egress`.

#### Scenario: Network option with build-only
- **WHEN** `--build-only --pi-ollama` is run
- **THEN** the build proceeds, and the CLI warns that `--pi-ollama` does not affect the image

#### Scenario: Plain build-only
- **WHEN** `--build-only` is run with build options only
- **THEN** no warning is printed, and the image carries both labels
