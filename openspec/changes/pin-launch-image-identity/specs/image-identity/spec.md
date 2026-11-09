# Spec Delta

## Purpose

Defines how sandbox images record the inputs and settings they were built for, when an existing image may be reused, how a session pins the exact image it launches, and what `--build-only` and `--no-build` guarantee.

## ADDED Requirements

### Requirement: Images are labeled with their inputs and settings
Every image the system builds SHALL carry an `org.project-sandbox.inputs` label and an `org.project-sandbox.settings` label. `inputs` is a fingerprint over every file in the session's generated build context plus the settings that affect the image. `settings` is a JSON summary of the policy-relevant settings. The `settings` label SHALL NOT contain secrets, such as API keys, proxy credentials or tokens.

#### Scenario: Labels on a fresh build
- **WHEN** a session builds its image
- **THEN** the image's `inputs` label equals the session's fingerprint, and its `settings` label lists the egress mode, extra domains, Ollama setting, proxy endpoints, CA certificate hashes, installed agents and base image

#### Scenario: Secret-free settings
- **WHEN** a session uses an agent proxy key or proxy credentials
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
After building or reusing its image, while still holding the build lock, a session SHALL add a tag unique to itself. It SHALL start its container from that tag. It SHALL remove the tag when the session ends. A later launch SHALL remove session tags whose sessions no longer run.

#### Scenario: Concurrent launch with a different policy
- **WHEN** session A releases the build lock, and session B rebuilds the project's tag with a different allowlist before A's container starts
- **THEN** A's container runs the image A built or verified, and B's runs its own

#### Scenario: Teardown
- **WHEN** a session ends normally, fails or is interrupted
- **THEN** its session tag is removed, and the image is kept while other tags or containers use it

#### Scenario: Killed session
- **WHEN** a session's CLI was killed, leaving its session tag behind
- **THEN** the next launch for any project removes that tag

### Requirement: --no-build verifies the image against the session's inputs
With `--no-build`, the system SHALL render the session's inputs, and compare its fingerprint with the image's `inputs` label before starting a container. On a mismatch or a missing label, the launch SHALL fail and name each differing setting from the `settings` label.

#### Scenario: Image built without Ollama
- **WHEN** an image was built with `--build-only --pi-ollama` but no `--agent pi`, and `--agent pi --pi-ollama --no-build` is launched
- **THEN** the launch fails before starting a container, stating that the image was built without the Pi Ollama setting

#### Scenario: Image from an older version
- **WHEN** `--no-build` names an image with no project-sandbox labels
- **THEN** the launch fails with an instruction to rebuild

#### Scenario: Matching image
- **WHEN** `--build-only --agent pi --pi-ollama` was run, followed by `--agent pi --pi-ollama --no-build` with the same other options
- **THEN** the session starts without building

### Requirement: --build-only builds the image a later session needs
`--build-only` SHALL accept `--agent` and the agent-dependent options, and SHALL build the image that a session with the same options would use. It SHALL keep rejecting a prompt and `--branch`. Without `--agent`, it SHALL warn about each agent-dependent option that has no effect.

#### Scenario: Build-only for Pi with Ollama
- **WHEN** `--build-only --agent pi --pi-ollama` is run
- **THEN** the image's firewall includes the Ollama rule, and its `settings` label records the Pi Ollama setting

#### Scenario: Ignored option warning
- **WHEN** `--build-only --pi-ollama` is run without `--agent`
- **THEN** the build proceeds, and the CLI warns that `--pi-ollama` has no effect without `--agent pi`
