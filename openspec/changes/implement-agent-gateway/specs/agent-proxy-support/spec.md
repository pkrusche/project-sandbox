## Purpose

Let a session route pi's or OpenCode's LLM traffic through a user-managed
local proxy that holds the real provider credentials, so the agent VM never
possesses a usable API key or OAuth credential. `project-sandbox` does not
start or manage the proxy; it configures the agent to use it.

## ADDED Requirements

### Requirement: Agent proxy is opt-in and limited to supported agents
The CLI SHALL accept an `--agent-proxy URL` flag. When the flag is absent,
all existing behavior SHALL be unchanged. The flag SHALL be accepted only
when the selected agent is `pi` or `opencode`; any other agent selection
SHALL be rejected with an error naming the supported agents, since Claude
Code and Codex CLI remain on the pass-through credential mechanism.

#### Scenario: Flag absent
- **WHEN** the user runs the CLI without `--agent-proxy`
- **THEN** no proxy forwarding resource is created, no firewall rule
  references a proxy endpoint, no agent receives proxy provider
  configuration, and credential forwarding behaves exactly as today

#### Scenario: Flag with a supported agent
- **WHEN** the user runs `--agent pi --agent-proxy http://127.0.0.1:3000`
  (or the same with `--agent opencode`)
- **THEN** the CLI accepts the flag and proceeds to configure that agent to
  reach the proxy

#### Scenario: Flag with an unsupported agent
- **WHEN** the user runs `--agent-proxy` with `--agent claude`, `--agent
  codex`, `--agent bash`, or no agent selection that resolves to pi or
  OpenCode
- **THEN** the CLI exits with an error before starting any container,
  stating that the agent proxy supports only `pi` and `opencode`

#### Scenario: Conflict with --pi-ollama
- **WHEN** the user runs `--agent pi --agent-proxy ... --pi-ollama`
- **THEN** the CLI exits with an error before starting any container,
  stating the two provider configurations are mutually exclusive

### Requirement: Proxy mode forbids credential forwarding into the agent VM
When `--agent-proxy` is set, the system SHALL behave as if
`--no-forward-credentials` were also set — no host agent credential is
staged, mounted, or forwarded into the agent VM — and SHALL reject
`--api-key-env` and `--api-key-env-file` as conflicting flags. The baked
provider configuration SHALL contain only a sentinel API-key value, never a
real credential.

#### Scenario: Proxy mode forces no credential forwarding
- **WHEN** the user runs with `--agent-proxy` and does not pass
  `--no-forward-credentials`
- **THEN** the session behaves as though `--no-forward-credentials` were
  set, and any previously staged credentials for the project are removed as
  that flag already specifies

#### Scenario: Explicit credential-injection flag is rejected
- **WHEN** the user runs with `--agent-proxy` together with `--api-key-env`
  or `--api-key-env-file`
- **THEN** the CLI exits with an error before starting any container,
  explaining that proxy mode and credential injection are mutually exclusive

#### Scenario: Agent VM holds only a sentinel
- **WHEN** a session runs with `--agent-proxy` and agent setup completes
- **THEN** the agent's baked provider configuration contains the proxy base
  URL and a sentinel key value, and no real provider API key or OAuth
  credential is present in the agent VM's environment or mounted files

### Requirement: Proxy URL is loopback-only and reached via runtime forwarding
The CLI SHALL accept only a loopback proxy URL (a `http://127.0.0.1:<port>`
shaped address) and SHALL reject other hosts. The agent VM SHALL reach the
proxy through the same verified runtime-specific loopback-forwarding
mechanism used for local Ollama support, applied to the proxy's port, under
a dedicated internal hostname. Runtimes with no verified forwarding strategy
SHALL fail with a clear unsupported-mode error before any container starts,
and SHALL NOT fall back to a wildcard listener or require the proxy to bind
beyond loopback.

#### Scenario: Non-loopback URL rejected
- **WHEN** the user passes `--agent-proxy http://0.0.0.0:3000` or a
  non-loopback hostname
- **THEN** the CLI exits with an error explaining the proxy must listen on
  host loopback

#### Scenario: Loopback proxy reachable through runtime strategy
- **WHEN** a supported runtime is selected and the proxy is listening on the
  given loopback port
- **THEN** the sandbox reaches the proxy through the selected forwarding
  path without the proxy changing its bind address

#### Scenario: No safe forwarding path
- **WHEN** the selected runtime mode provides neither verified native
  forwarding nor a safe, host-bindable bridge address for the proxy port
- **THEN** startup fails with a clear unsupported-mode error and no
  container is started

### Requirement: Preflight reachability check fails fast
Before starting any container (outside `--dry-run`), the system SHALL
attempt a bounded TCP connection to the configured proxy URL and SHALL abort
the run with an actionable error, referencing the agent-proxy documentation,
if the proxy is not accepting connections. The system SHALL NOT attempt to
start, restart, or otherwise manage the proxy process.

#### Scenario: Proxy not running
- **WHEN** the user runs with `--agent-proxy` and nothing is listening at
  the configured URL
- **THEN** the CLI aborts before starting any container with an error that
  names the URL and points at the local proxy setup documentation

#### Scenario: Proxy dies mid-session
- **WHEN** the proxy stops after the agent VM has started
- **THEN** the agent's requests fail, and the system does not restart the
  proxy or fall back to direct provider credentials

### Requirement: Firewall scopes access to the proxy endpoint
When the proxy is active and the firewall is enabled, the rendered firewall
rules SHALL include a port-scoped allow rule for the forwarded proxy
endpoint, following the same pattern as the existing Ollama endpoint rule,
and SHALL leave all other firewall behavior unchanged. When the firewall is
disabled, the CLI SHALL warn that the baked internal proxy hostname will not
resolve, mirroring the existing Ollama warning.

#### Scenario: Proxy endpoint allow rule
- **WHEN** the agent VM starts with `--agent-proxy` and the firewall enabled
- **THEN** the rendered firewall rules permit outbound TCP to the forwarded
  proxy endpoint on its port, and the remaining allowlist is the same as an
  equivalent non-proxy session

#### Scenario: Non-proxy sessions unaffected
- **WHEN** a session runs without `--agent-proxy`
- **THEN** the rendered firewall rules contain no proxy endpoint rule

#### Scenario: Firewall disabled with proxy active
- **WHEN** the user runs with `--agent-proxy` and the firewall disabled
- **THEN** the CLI prints a warning that the baked proxy hostname relies on
  firewall initialization and will not resolve inside the container

### Requirement: Proxy models are named explicitly
The CLI SHALL accept a repeatable `--agent-proxy-model ID` flag naming the
models the proxy exposes, SHALL require at least one model when
`--agent-proxy` is set, and SHALL bake the given models into the generated
provider configuration with the first model as the default where the agent
requires one.

#### Scenario: No model named
- **WHEN** the user runs with `--agent-proxy` and no `--agent-proxy-model`
- **THEN** the CLI exits with an error asking for at least one model name

#### Scenario: Multiple models named
- **WHEN** the user passes `--agent-proxy-model` more than once
- **THEN** every named model appears in the generated provider
  configuration and the first is used as the default model

### Requirement: Pi is configured to use the proxy
When `--agent pi --agent-proxy` is active, the system SHALL bake a pi
custom-provider configuration (`models.json`) whose base URL points at the
proxy's forwarded endpoint with the configured model list and a sentinel
API key, and SHALL bake `settings.json` selecting that provider and its
default model, following the same generated-config shape as the existing
Ollama support.

#### Scenario: Pi provider config baked
- **WHEN** a session runs `--agent pi --agent-proxy ...`
- **THEN** the generated `models.json` defines a provider whose base URL is
  the proxy's in-container endpoint, listing the configured models with a
  sentinel key, and `settings.json` sets that provider and the first model
  as defaults

### Requirement: OpenCode is configured to use the proxy
When `--agent opencode --agent-proxy` is active, the system SHALL generate
an OpenCode configuration defining a custom provider whose `baseURL` points
at the proxy's forwarded endpoint with the configured models and a sentinel
key, such that the existing `--model <provider>/<model>` selection works
against it.

#### Scenario: OpenCode provider config generated
- **WHEN** a session runs `--agent opencode --agent-proxy ...`
- **THEN** the generated OpenCode configuration defines a custom provider
  with the proxy `baseURL`, the configured models, and a sentinel key, and
  contains no real provider credential

### Requirement: Dry run previews without side effects
When `--dry-run` is combined with `--agent-proxy`, the CLI SHALL print the
provider configuration it would bake and the commands it would run, SHALL
write no files and start no container or forwarding resource, and SHALL NOT
require the proxy to be running.

#### Scenario: Dry run with proxy flags
- **WHEN** the user runs `--dry-run --agent-proxy ... --agent-proxy-model m1`
- **THEN** the CLI prints the baked provider configuration and planned
  commands, performs no reachability check, and writes nothing
