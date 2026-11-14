# Spec Delta

## Purpose

Lets project-sandbox run sandboxes in a user-created Colima VM through its Docker engine, targeting that VM explicitly without changing the user's Docker context or managing the VM's lifecycle.

## ADDED Requirements

### Requirement: Colima is an explicit runtime
The CLI SHALL accept `--runtime colima`. It SHALL NOT select Colima under `--runtime auto`, and Apple `container` SHALL keep its precedence there. `--colima-profile NAME` SHALL select the profile, defaulting to `project-sandbox`. The CLI SHALL reject `--colima-profile` with any other runtime, and SHALL reject a profile name that is not a valid Colima profile name before any container work.

#### Scenario: Colima is selected
- **WHEN** the user runs with `--runtime colima`
- **THEN** the CLI plans the sandbox on the Docker engine of the `project-sandbox` profile

#### Scenario: Auto never picks Colima
- **WHEN** the user runs with `--runtime auto` on a Mac that has Colima running and no Apple `container`
- **THEN** the CLI does not select Colima

#### Scenario: Profile flag with another runtime
- **WHEN** the user passes `--colima-profile work` with `--runtime docker`
- **THEN** the CLI exits with an error naming the conflict and performs no container work

### Requirement: Every Docker operation targets the profile explicitly
For the Colima runtime, every Docker invocation, including image builds, image inspection, sidecar and network management, logs and cleanup, SHALL target the profile's Docker context (`colima-<profile>`) explicitly. The CLI SHALL NOT change the user's current Docker context or `DOCKER_HOST`, and SHALL NOT read the ambient `DOCKER_CONTEXT` or `DOCKER_HOST` to choose an engine.

#### Scenario: Context is explicit
- **WHEN** a Colima launch builds the image and runs the sandbox
- **THEN** each `docker` invocation includes `--context colima-project-sandbox`

#### Scenario: User context is untouched
- **WHEN** the user's current context is `default` before and during a Colima launch
- **THEN** it is still `default` afterwards, and `DOCKER_HOST` set in the environment is not used to reach the profile

### Requirement: The profile is verified before any container work
Before building, creating a network or starting a container, a non-dry-run Colima launch SHALL verify that the profile's Docker context exists, that its engine answers, and that the engine belongs to the named Colima profile. When it does not, the CLI SHALL exit naming the cause and the command to run (`colima start --profile NAME` when the profile is stopped or the context is missing). It SHALL NOT create, start, stop, reset or reconfigure a Colima VM.

#### Scenario: Profile is stopped
- **WHEN** the profile's context exists but its engine does not answer
- **THEN** the CLI exits before any container work and names `colima start --profile project-sandbox`

#### Scenario: Context is not Colima's
- **WHEN** a context named `colima-project-sandbox` points at an engine that is not that Colima profile's VM
- **THEN** the CLI exits before any container work and says the context is not recognized

#### Scenario: Dry run
- **WHEN** the user passes `--dry-run`
- **THEN** the CLI prints the Docker commands it would run, with the context argument, and performs no Colima, Docker or file operation

### Requirement: Mounts must be visible in the VM
Before starting a Colima sandbox, the CLI SHALL check that each host path it mounts (workspace, generated staging inputs, credential files, and the main repository's `.git` for `--branch`) is visible inside the VM at the same path. A path the VM does not expose SHALL cause an error before the sandbox starts, naming the path and the Colima mount to add. Mounts that the user has not granted SHALL NOT be broadened by project-sandbox.

#### Scenario: Workspace outside the VM's mounts
- **WHEN** the workspace is in a directory that the Colima profile does not mount
- **THEN** the CLI exits before the sandbox starts, naming the directory and the Colima mount setting

#### Scenario: Visible workspace
- **WHEN** the workspace and every generated mount source are inside the profile's mounts
- **THEN** the launch proceeds and the sandbox sees the workspace at its usual location

### Requirement: Host-dependent features are unsupported on Colima
The CLI SHALL reject, before any container work, a Colima launch that requires a path from the sandbox to the macOS host: an enabled local Ollama service, `--agent-proxy` to a macOS-local gateway, and an upstream parent proxy that listens on host loopback. Each error SHALL name the feature and say that it is unsupported on Colima. An upstream parent proxy that is reachable by network address from the VM SHALL remain supported, and no rejection SHALL be bypassed by widening sandbox networking.

#### Scenario: Ollama is enabled
- **WHEN** a Colima launch has local Ollama enabled
- **THEN** the CLI exits before any container work and names Ollama as unsupported on Colima

#### Scenario: Loopback parent proxy
- **WHEN** `egress.toml` sets `[egress.upstream] url = "http://127.0.0.1:3128"` and a Colima sidecar-mode launch starts
- **THEN** the CLI exits before the sidecar starts, names the loopback parent as unsupported on Colima, and suggests an address reachable from the VM

#### Scenario: Network-reachable parent proxy
- **WHEN** `egress.toml` sets `[egress.upstream] url = "https://proxy.corp.example"`
- **THEN** the sidecar starts with that parent and sidecar-mode sandboxes reach it through the sidecar

### Requirement: Firewall egress on Colima matches plain Docker
`--egress firewall` on Colima SHALL render and enforce the same firewall as on Docker Engine, using the profile's default bridge network. The CLI SHALL document that the private-network isolation applies to sidecar mode only.

#### Scenario: Firewall mode
- **WHEN** the user runs `--runtime colima --egress firewall`
- **THEN** the sandbox runs on the profile's default bridge network with the same firewall policy as `--runtime docker`
