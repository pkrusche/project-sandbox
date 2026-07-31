## Purpose

Let a session route its coding agent's LLM traffic through an `agentgateway`
sidecar that holds the real provider credential, so the agent VM never
possesses a usable API key or OAuth credential.

## ADDED Requirements

### Requirement: Gateway is opt-in via a dedicated flag
The CLI SHALL accept a `--gateway[=auto|on|off]` flag. `off` (the default when
no `.project-sandbox/gateway/` directory exists) SHALL leave all existing
behavior unchanged. `auto` SHALL behave as `on` when
`.project-sandbox/gateway/` exists and as `off` otherwise. `on` SHALL fail the
run with a clear error if the sidecar cannot be started or does not pass its
health check, rather than silently falling back to direct provider access.

#### Scenario: Flag absent
- **WHEN** the user runs the CLI without `--gateway`
- **THEN** no gateway network or container is created, no firewall rule
  references a gateway endpoint, and no agent receives gateway base-URL
  configuration

#### Scenario: auto with no gateway directory
- **WHEN** the user runs with `--gateway auto` (or omits the value) and
  `.project-sandbox/gateway/` does not exist
- **THEN** the session proceeds exactly as if `--gateway off` were passed

#### Scenario: on fails closed
- **WHEN** the user runs with `--gateway on` and the sidecar fails to start or
  does not pass its health check within the configured timeout
- **THEN** the CLI aborts the run before starting the agent VM and reports the
  failure; no agent VM is started without the gateway in place

### Requirement: Gateway mode forbids credential forwarding into the agent VM
When `--gateway on` (or `auto` resolving to on) is set, the system SHALL
behave as if `--no-forward-credentials` were also set, SHALL reject any
attempt to override this with an explicit credential-forwarding flag, and
SHALL fail preflight if any known agent credential path is already staged
into the agent home for this session.

#### Scenario: Gateway forces no credential forwarding
- **WHEN** the user runs with `--gateway on` and does not pass
  `--no-forward-credentials`
- **THEN** the session behaves as though `--no-forward-credentials` were set:
  no host agent credential is staged, mounted, or forwarded into the agent VM

#### Scenario: Explicit conflicting flag is rejected
- **WHEN** the user runs with `--gateway on` together with a flag or option
  that would stage or forward a host credential into the agent VM
- **THEN** the CLI exits with an error before starting any container,
  explaining that gateway mode and credential forwarding are mutually
  exclusive

#### Scenario: Preflight tripwire
- **WHEN** `--gateway on` is set and, due to any code path, a known credential
  file would be written into the agent container's home directory or mounted
  read-write or read-only from a credential-bearing host path
- **THEN** the run aborts before the agent VM starts

### Requirement: Gateway configuration is generated, not hand-authored
The system SHALL render an agentgateway configuration file from the project's
`gateway` settings and enabled providers, referencing every credential value
as an environment-variable substitution (e.g. `$ANTHROPIC_API_KEY`) rather
than a literal value, and SHALL validate the rendered file is well-formed
before mounting it into the gateway VM.

#### Scenario: Rendered config contains no literal secrets
- **WHEN** the gateway config is rendered for one or more enabled providers
- **THEN** the output file contains only `$ENV_VAR`-style credential
  references, never a literal API key or token value

#### Scenario: Invalid generated config is rejected before container start
- **WHEN** the rendered configuration fails schema/structure validation
- **THEN** the run aborts with a validation error and no gateway or agent
  container is started

#### Scenario: Dry run renders and redacts, writes nothing
- **WHEN** the user runs with `--dry-run --gateway on`
- **THEN** the CLI prints the gateway config and the container/network
  commands it would run, redacts every secret value as `<redacted>`, and
  starts no container, network, or file write

### Requirement: Secrets are partitioned and never reach the agent VM
The system SHALL read gateway secrets from a single host env file (private
mode, default `~/.config/project-sandbox/secrets.env`, overridable per
project or via flag), SHALL make secret values available only to the gateway
container's process environment, and SHALL NOT copy, mount, or inject any
partitioned secret value into the agent VM in any form (environment variable,
mounted file, or baked config).

#### Scenario: Secrets file permissions are enforced
- **WHEN** the configured secrets env file is not private to the user (mode
  stricter than what the file actually has)
- **THEN** the CLI refuses to use the file and reports the permission problem
  rather than silently reading it

#### Scenario: Agent VM environment has no provider keys
- **WHEN** a session runs with `--gateway on` and completes agent VM setup
- **THEN** inspecting the agent VM's environment and mounted files shows no
  raw provider API key or OAuth credential value, only a sentinel token and
  base-URL configuration

#### Scenario: Gateway VM receives secrets read-only
- **WHEN** the gateway container starts
- **THEN** it receives the partitioned secret values (via its process
  environment or a read-only mount) and the agent VM has no access to that
  mount or those values

### Requirement: Gateway sidecar has a bounded, health-gated lifecycle
The system SHALL create an isolated per-project network for the gateway and
agent VMs, start the gateway container on it, discover its assigned address,
and confirm the gateway is accepting connections before starting or
continuing to run the agent VM. On session end, the system SHALL stop the
gateway container and remove the network it created.

#### Scenario: Gateway starts before the agent VM
- **WHEN** a session with `--gateway on` begins
- **THEN** the gateway container is created and health-checked before the
  agent VM is started, and the agent VM is configured with the discovered
  gateway address

#### Scenario: Gateway health check times out
- **WHEN** the gateway container does not become reachable within the
  configured timeout
- **THEN** the run aborts, the agent VM is never started, and any
  already-created gateway container and network are cleaned up

#### Scenario: Teardown on normal exit
- **WHEN** the session ends normally, is interrupted, or the agent VM fails
  to start
- **THEN** the gateway container is stopped and the per-project gateway
  network is removed

#### Scenario: Unsupported platform
- **WHEN** `--gateway on` (or `auto` resolving to on) is requested on a
  platform or runtime that cannot provide inter-container networking (for
  example Apple `container` below macOS 26, or the `chroot` runtime)
- **THEN** the CLI fails before creating any gateway resources, with an error
  naming the unsupported platform/runtime

### Requirement: Agent VM egress collapses to the gateway only
When the gateway is active and the firewall is enabled, the agent VM's egress
allowlist SHALL permit outbound traffic only to the discovered gateway
address and port, with default-deny for all other destinations; the gateway
VM keeps its own separate allowlist covering the enabled providers' domains.

#### Scenario: Firewall allows only the gateway
- **WHEN** the agent VM starts with the gateway active and the firewall
  enabled
- **THEN** the rendered firewall rules permit outbound TCP only to the
  gateway's address and port, and deny outbound traffic to provider domains
  directly from the agent VM

#### Scenario: Gateway becomes unreachable mid-session
- **WHEN** the gateway container stops or becomes unreachable after the agent
  VM has started
- **THEN** the agent VM's firewall configuration continues to block direct
  egress to LLM providers (fails closed, not open)

### Requirement: Supported agents are configured to reach the gateway
For each enabled agent that this capability supports (Claude Code, Codex CLI,
OpenCode), the system SHALL generate that agent's base-URL configuration to
point at the gateway's per-provider route and SHALL supply a sentinel
credential value in place of a real provider key. For agents or provider
combinations this capability cannot route through a gateway, the system
SHALL emit a clear warning instead of silently proceeding as if unrouted.

#### Scenario: Claude Code routed through the gateway
- **WHEN** a session runs `--agent claude --gateway on`
- **THEN** the generated Claude configuration sets a gateway base URL and a
  sentinel auth token, and no direct Anthropic API key is present in that
  configuration

#### Scenario: Codex CLI routed through the gateway
- **WHEN** a session runs `--agent codex --gateway on`
- **THEN** the generated Codex `config.toml` defines a custom model provider
  pointing at the gateway's Codex route with a sentinel credential

#### Scenario: OpenCode routed through the gateway
- **WHEN** a session runs `--agent opencode --gateway on`
- **THEN** the generated OpenCode configuration defines a custom provider
  with a `baseURL` pointing at the gateway and a sentinel credential

#### Scenario: Unsupported agent/provider combination warns rather than silently bypassing
- **WHEN** a session requests `--gateway on` with an agent or provider
  combination this capability cannot route through the gateway (for example
  GitHub-hosted Copilot models)
- **THEN** the CLI prints a warning naming the unsupported combination before
  proceeding, rather than starting the agent as if it were routed
