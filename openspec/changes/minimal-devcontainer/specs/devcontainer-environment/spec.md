# Spec Delta

## Purpose

Defines what the generated devcontainer provides: a test and debug environment with the project's dependencies. It covers the devcontainer's network modes and privileges, and the absence of agents and credentials.

## ADDED Requirements

### Requirement: The devcontainer has no network by default
The generated devcontainer SHALL run with no network interfaces other than loopback, unless the user opts in. It SHALL set the offline variables of the generated toolchains: `UV_OFFLINE=1`, `CARGO_NET_OFFLINE=true` and `npm_config_offline=true`.

#### Scenario: Default devcontainer
- **WHEN** a launch renders the devcontainer without `--devcontainer-network`
- **THEN** `devcontainer.json` passes `--network none` in `runArgs`, and its `containerEnv` contains the offline variables

#### Scenario: Outbound connection from a test
- **WHEN** code inside the default devcontainer opens a TCP connection to any non-loopback address
- **THEN** the connection fails immediately

#### Scenario: Loopback services
- **WHEN** a test inside the devcontainer starts a server on `127.0.0.1` and connects to it
- **THEN** the connection succeeds

### Requirement: Network access is an explicit, unfiltered opt-in
With `--devcontainer-network=runtime`, the devcontainer SHALL use the runtime's default network, without a firewall and without added capabilities. When `--internet-proxy` is given, its proxy environment SHALL be applied. The launch SHALL warn that the devcontainer's network is unfiltered, and SHALL NOT set the offline variables. `--dry-run` SHALL show the selected devcontainer network mode.

#### Scenario: Opt-in network
- **WHEN** a launch uses `--devcontainer-network=runtime`
- **THEN** `devcontainer.json` has no `--network` override, no `--cap-add` and no firewall step, and the CLI prints the unfiltered-network warning

#### Scenario: Invalid value
- **WHEN** `--devcontainer-network=bridge` is given
- **THEN** the command fails during argument validation, listing `none` and `runtime`

### Requirement: The devcontainer runs without elevated privileges
The devcontainer SHALL NOT add Linux capabilities, SHALL NOT include any sudoers rule, and SHALL NOT run any command as root after the container starts.

#### Scenario: No privilege escalation path
- **WHEN** the devcontainer is running
- **THEN** `runArgs` contains no `--cap-add`, `/etc/sudoers.d/` contains no project-sandbox rule, and `postStartCommand` runs no `sudo`

#### Scenario: No firewall script in the image
- **WHEN** the devcontainer image is built
- **THEN** it contains no `project-sandbox-init-firewall`

### Requirement: The devcontainer contains no agents or agent credentials
The devcontainer image SHALL NOT install agent CLIs. `devcontainer.json` SHALL NOT mount staged credentials, agent configs or Claude history, SHALL NOT set agent-specific environment variables, and SHALL NOT recommend agent IDE extensions.

#### Scenario: Credential forwarding is irrelevant
- **WHEN** a launch that forwards credentials renders the devcontainer
- **THEN** `devcontainer.json` references no `/project-sandbox-secrets`, `/project-sandbox-config` or credential staging path, and no staging is created for the devcontainer

#### Scenario: Image contents
- **WHEN** the devcontainer image is built
- **THEN** `claude`, `codex`, `opencode`, `pi` and `copilot` are not on `PATH`

#### Scenario: IDE customizations
- **WHEN** `devcontainer.json` is rendered
- **THEN** `customizations.vscode.extensions` contains no Claude or Codex extension

### Requirement: Developer conveniences are retained
The devcontainer SHALL keep:
- the same base image and project dependency layers as the direct session image;
- the user's git identity;
- persisted bash history;
- the user's `--mount` values;
- the `--memory`/`--cpus` limits.

#### Scenario: Dependencies available offline
- **WHEN** a `--python-uv` project's devcontainer starts with no network
- **THEN** `uv run pytest` runs using the dependencies baked into the image

#### Scenario: Shell history
- **WHEN** the user runs commands in the devcontainer and later rebuilds it
- **THEN** the earlier commands are in bash history

### Requirement: Devcontainer configuration is hidden inside the devcontainer
The devcontainer SHALL mount empty read-only directories over `/workspace/.devcontainer` and `/workspace/.project-sandbox`, so that code running in it cannot read or change generated configuration, including the host-executed `initializeCommand`.

#### Scenario: Test code tries to edit devcontainer.json
- **WHEN** code in the devcontainer writes to `/workspace/.devcontainer/devcontainer.json`
- **THEN** the write fails, and the host file is unchanged

### Requirement: The devcontainer is rendered independently of egress mode
The devcontainer SHALL be rendered for every container-runtime launch, in firewall and sidecar egress modes alike. Chroot launches SHALL NOT render it.

#### Scenario: Sidecar-mode launch
- **WHEN** a launch uses `--egress sidecar`
- **THEN** the devcontainer is rendered with the same content as in firewall mode

#### Scenario: Chroot launch
- **WHEN** a launch uses `--runtime chroot`
- **THEN** no devcontainer is rendered
