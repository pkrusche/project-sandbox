# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: Local-service forwarding needs a runtime-provided host alias`
- TO: `### Requirement: Local-service forwarding works on every supported runtime`

## MODIFIED Requirements

### Requirement: Local-service forwarding works on every supported runtime
The sidecar SHALL forward configured local services to the host's loopback on Apple `container` (with the configured localhost alias), Docker Desktop, Podman machine, Docker Engine, and rootless Podman with pasta on Podman 5.3 or later. On rootless Docker, on rootless Podman with slirp4netns, and on rootless Podman with pasta older than 5.3, starting a sidecar with configured forwarders SHALL fail and name the requirement. Starting a sidecar without forwarders SHALL work there.

#### Scenario: Docker Desktop
- **WHEN** the sidecar starts on Docker Desktop with forwarded port 11434
- **THEN** the forwarder reaches the host's Ollama through `host.docker.internal`

#### Scenario: Docker Engine with forwarders
- **WHEN** the sidecar starts on Docker Engine with forwarded port 11434 and Ollama listens only on the host's `127.0.0.1`
- **THEN** a sandbox using Ollama reaches it through the sidecar

#### Scenario: Rootless Podman with pasta
- **WHEN** the sidecar starts on rootless Podman 5.3 or later with pasta and forwarded port 8787, and the agent proxy listens only on the host's `127.0.0.1`
- **THEN** a sandbox using the agent proxy reaches it through the sidecar

#### Scenario: Unsupported Linux network mode
- **WHEN** the sidecar is to start with a configured forwarder on rootless Podman with slirp4netns, or on Podman older than 5.3
- **THEN** startup fails before any container is created and names the Podman version and network mode that forwarding requires

## ADDED Requirements

### Requirement: Docker Engine host forwarders are reachable only by the sidecar
On Docker Engine, the sidecar SHALL reach the Internet through a dedicated, non-internal uplink network that no other container joins. The host forwarders SHALL listen only on that network's gateway address and SHALL close connections from any address other than the sidecar's. They SHALL connect only to host loopback on the same port, and the sidecar SHALL resolve `host.docker.internal` to the gateway address.

#### Scenario: Other containers cannot use the forwarders
- **WHEN** a container on Docker's default `bridge` network connects to the uplink gateway on a forwarded port
- **THEN** the connection fails

#### Scenario: Wrong peer on the uplink
- **WHEN** a connection to a host forwarder comes from an address other than the sidecar's
- **THEN** the forwarder closes it without connecting to the host service

### Requirement: Host forwarders are supervised by the sidecar
Host forwarders SHALL run in the sidecar-owned host process that also runs the relay. They SHALL start with the sidecar, follow `project-sandbox-egress forward add|remove` without a restart, and stop with `project-sandbox-egress down`. A sidecar-mode launch SHALL restart the host process when it is no longer running, and `status` SHALL report each host forwarder.

#### Scenario: Forwarder added live on Docker Engine
- **WHEN** the sidecar runs on Docker Engine and the user runs `project-sandbox-egress forward add 8787`
- **THEN** the host forwarder for 8787 listens on the uplink gateway and the in-sidecar forwarder listens on the internal network, without a sidecar restart

#### Scenario: Host process died
- **WHEN** the host process was killed and a sidecar-mode sandbox is launched on Docker Engine
- **THEN** the launch restarts it, with every configured forwarder, before starting the sandbox

### Requirement: Sidecar mode is verified on Linux hosts
The opt-in `egress-sidecar` e2e suite SHALL run on Docker Engine and on rootless Podman with pasta. On both it SHALL cover the policy probes, two concurrent sandboxes on one sidecar, and local-service forwarding to a service listening only on host loopback.

#### Scenario: Linux run
- **WHEN** the `egress-sidecar` suite runs on a Linux host with Docker Engine or rootless Podman
- **THEN** it reports the policy probes, the shared-sidecar scenarios and the loopback-only forwarding probe for that runtime
