# colima-runtime Specification

## Purpose

Lets project-sandbox run sandboxes in a user-created Colima VM through its Docker engine, targeting that VM explicitly without changing the user's Docker context or managing the VM's lifecycle.

## Requirements

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
Before starting a Colima sandbox, the CLI SHALL check that each host path it mounts (workspace, generated inputs, credentials, `--mount` values, and `.git` for `--branch`) is visible in the VM at the same path. A path the VM does not expose SHALL fail the launch before the sandbox starts, naming the path and a `colima start` command that also lists the profile's current exports. It SHALL NOT broaden mounts the user has not granted.

#### Scenario: Workspace outside the VM's mounts
- **WHEN** the workspace is in a directory that the Colima profile does not mount
- **THEN** the CLI exits before the sandbox starts, naming the directory and the Colima mount setting

#### Scenario: Staging outside the exported tree
- **WHEN** a generated mount source, such as a hidden-VCS mask directory, would be created in the macOS temporary directory
- **THEN** on Colima it is created under the project-sandbox state directory instead, and the launch does not fail on a missing bind source

#### Scenario: The suggested command keeps the current exports
- **WHEN** the VM exports `/Users/me/src` read-write and a launch needs `/private/tmp`
- **THEN** the suggested command passes both `--mount /Users/me/src:w` and `--mount /private/tmp:w`, and says that `--mount` replaces the profile's mounts

#### Scenario: Current exports cannot be read
- **WHEN** the VM's exports cannot be listed
- **THEN** the message says so and does not suggest a command that names only the new path

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

### Requirement: Doctor reports a Colima profile without changing it
`project-sandbox doctor` SHALL check a Colima profile read-only when `colima` is on `PATH` or `--colima-profile NAME` is given: that its Docker context is the profile's, that its engine answers, whether the VM has fewer CPUs than a sandbox asks for by default, whether the current and state directories are visible in the VM, and whether the project's networks and sidecar are healthy. It SHALL NOT start the VM, create a network or change the Docker context.

#### Scenario: Profile was never started
- **WHEN** the profile's Docker context does not exist
- **THEN** doctor reports it as informational and names `colima start --profile NAME`

#### Scenario: VM smaller than the sandbox default
- **WHEN** the VM has 2 CPUs and a sandbox asks for 4 by default
- **THEN** doctor warns, and names both `--cpus 2` and restarting the profile with more CPUs

#### Scenario: Directory not visible in the VM
- **WHEN** the current directory is not on a host-backed mount of the VM
- **THEN** doctor warns, names the directory and the Colima mount to add

#### Scenario: Sidecar detached from the sandbox network
- **WHEN** the sidecar runs but is not attached to the sandbox network as `egress`
- **THEN** doctor warns that the next launch recreates it
