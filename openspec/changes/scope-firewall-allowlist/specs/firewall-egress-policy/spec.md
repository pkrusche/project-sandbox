# Spec Delta

## Purpose

Defines what a firewall-mode sandbox may reach: per-agent provider hosts, ports, the handling of non-global addresses, user-added destinations, and how the firewall policy is delivered to the sandbox at runtime rather than built into the image.

## ADDED Requirements

### Requirement: The firewall policy is a per-session runtime input
The system SHALL render each session's firewall script into that session's private staging, and mount it read-only into the container. Privileged setup SHALL run the mounted script. Images SHALL NOT contain a firewall script or any network allowlist. A session with the firewall enabled SHALL fail before starting the session command if the mounted script is absent.

#### Scenario: Switching agents does not rebuild
- **WHEN** a Claude session is followed by a Codex session for the same project with otherwise identical options
- **THEN** the second session reuses the image, and its firewall admits the Codex provider hosts

#### Scenario: Missing script
- **WHEN** the firewall is enabled and `/project-sandbox-firewall/init-firewall.sh` is not mounted
- **THEN** the container exits non-zero before the session command starts

### Requirement: Provider hosts are scoped to the session's agent
In firewall mode without an agent proxy, the system SHALL admit only the selected agent's provider hosts, from a single table that defines each agent's set. `--agent bash` SHALL admit the union of every agent's set. The sidecar's always-allowed provider hosts SHALL equal that union.

#### Scenario: Headless Claude
- **WHEN** a headless Claude session runs in firewall mode
- **THEN** the Anthropic provider hosts are admitted, and no OpenAI host is

#### Scenario: Interactive Codex
- **WHEN** an interactive `--agent codex` session runs in firewall mode
- **THEN** only the OpenAI provider hosts are admitted

#### Scenario: Shell session
- **WHEN** an interactive `--agent bash` session runs in firewall mode
- **THEN** every agent's provider hosts are admitted

#### Scenario: Pi with Ollama
- **WHEN** `--agent pi --pi-ollama` runs in firewall mode
- **THEN** no remote provider host is admitted, and Ollama is reachable on its port-scoped rule

#### Scenario: Sidecar alignment
- **WHEN** the sidecar's effective allowlist is computed
- **THEN** its provider hosts equal the union of the firewall's per-agent sets

### Requirement: Pinned destinations are port-scoped
Provider hosts and `--extra-domain` hosts SHALL be admitted only as resolved-address and TCP-port pairs. The port SHALL be 443, unless `--extra-domain host:port` names another TCP port. Other protocols and ports to those addresses SHALL be rejected. Outbound DNS SHALL remain blocked.

#### Scenario: Other port on a provider address
- **WHEN** a process connects to a pinned provider address on TCP port 80 or 22
- **THEN** the connection is rejected

#### Scenario: Extra domain with a port
- **WHEN** `--extra-domain git.example.com:22` is given
- **THEN** `git.example.com`'s addresses are admitted on TCP 22 only

#### Scenario: Invalid port
- **WHEN** `--extra-domain example.com:0` or `example.com:99999` is given
- **THEN** the command fails during argument validation

### Requirement: Non-global addresses are never pinned implicitly
DNS answers for provider and extra-domain hosts SHALL be classified before pinning. Non-global answers SHALL be dropped with a warning naming the host and address: loopback, private, link-local, shared (CGNAT), documentation, multicast, reserved and unspecified, including IPv4-mapped and NAT64-embedded forms. A host-and-port pair given with `--allow-private-destination` SHALL be admitted, on exactly that port, even when it resolves to a non-global address.

#### Scenario: Extra domain resolving to metadata
- **WHEN** `--extra-domain evil.example` resolves to `169.254.169.254`
- **THEN** that address is not admitted, and the firewall output warns about it

#### Scenario: Internal mirror
- **WHEN** `--allow-private-destination mirror.corp:8443` is given and it resolves to `10.1.2.3`
- **THEN** `10.1.2.3` is admitted on TCP 8443 only

#### Scenario: Mapped address
- **WHEN** a host resolves to `::ffff:127.0.0.1` or `64:ff9b::a01:203`
- **THEN** the address is treated as non-global and not admitted

### Requirement: --allow-github is deprecated and has no effect
In firewall mode, the CLI SHALL accept `--allow-github` and SHALL warn that it is deprecated and ignored, suggesting `--extra-domain` for specific hosts. The firewall SHALL NOT admit GitHub hosts or CIDR ranges unless they are given with `--extra-domain`. Sidecar mode keeps rejecting `--allow-github`, as before.

#### Scenario: Launcher passes --allow-github
- **WHEN** a launch includes `--allow-github`
- **THEN** the session starts, the deprecation warning is printed, and no GitHub host is admitted
