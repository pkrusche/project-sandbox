## Purpose

Route pi or OpenCode LLM traffic through the authenticated local service
configured by `pkrusche/agentgateway-locally`, keeping provider API keys and
host OAuth credentials out of the agent VM while admitting only the narrower
gateway bearer key required to use that service.

## ADDED Requirements

### Requirement: Proxy mode is opt-in and limited to supported agents

The CLI SHALL accept `--agent-proxy URL` and `--agent-proxy-key-env NAME` and
SHALL reuse the regular `--model` option. Proxy mode SHALL be accepted only for
pi and OpenCode and SHALL require `--model`. When absent, all existing behavior
SHALL remain unchanged.

#### Scenario: Supported agent opts in
- **WHEN** the user selects pi or OpenCode with proxy URL
  `http://127.0.0.1:4000/v1`, a gateway-key environment variable, and a regular
  `--model` selection
- **THEN** the CLI configures that agent to use the local proxy

#### Scenario: Regular model selection is required
- **WHEN** proxy mode is requested without `--model`
- **THEN** the CLI exits before secret lookup, network, or container work and
  asks for the regular model option

#### Scenario: OpenCode model names the generated provider
- **WHEN** OpenCode proxy mode receives a `--model` value without the
  `agent-proxy/` prefix or with another provider prefix
- **THEN** the CLI rejects it and shows the required
  `agent-proxy/<model-id>` form

#### Scenario: Unsupported agent is rejected
- **WHEN** proxy mode is requested with Claude, Codex, bash, or another agent
- **THEN** the CLI exits before container work and names pi and OpenCode as the
  supported agents

#### Scenario: Existing behavior is unchanged
- **WHEN** no proxy URL is supplied
- **THEN** no proxy preflight, forwarding, firewall rule, credential handling,
  or provider configuration occurs

### Requirement: Provider credentials are excluded while the gateway key is admitted

Proxy mode SHALL force `--no-forward-credentials` behavior and reject
`--api-key-env` and `--api-key-env-file`. No provider API key or host agent
OAuth credential SHALL enter the agent VM. The gateway bearer key named by
`--agent-proxy-key-env` SHALL be the sole LLM credential admitted by proxy
mode and SHALL be staged only for the selected agent.

#### Scenario: Provider credentials are excluded
- **WHEN** proxy mode prepares a session
- **THEN** stale staged credentials are purged and no OpenAI/Anthropic key,
  host agent credential file, or unrelated credential mount is included

#### Scenario: Gateway key is present where required
- **WHEN** a supported agent is configured for an authenticated proxy
- **THEN** its private provider configuration can authenticate with the gateway
  key and no other agent profile or general environment receives that key

#### Scenario: Generic credential injection conflicts
- **WHEN** proxy mode is combined with `--api-key-env` or
  `--api-key-env-file`
- **THEN** the CLI rejects the invocation before reading or staging secrets

### Requirement: Gateway key input and output handling is secret-safe

The gateway key SHALL be loaded from the host environment variable named by
`--agent-proxy-key-env`, defaulting to `AGENTGATEWAY_API_KEY`. If it is empty,
the system SHALL capture `pass show agentgateway-api-key`; it SHALL never accept
the secret as a command-line value. Real runs SHALL fail if neither source
produces a value. The system SHALL NOT expose the value in argv, logs, errors,
dry-run output, or transcripts, and SHALL protect and clean secret-bearing
staged files using the existing private credential lifecycle.

#### Scenario: Environment key is absent and pass succeeds
- **WHEN** a real proxy run finds the selected key environment variable empty
  and `pass show agentgateway-api-key` succeeds
- **THEN** the CLI uses the captured value without exposing it in argv or output

#### Scenario: Both gateway key sources fail
- **WHEN** the selected environment variable is empty and the `pass` lookup
  fails or returns empty output
- **THEN** the CLI exits before network or container work with an error naming
  the attempted sources but no secret value

#### Scenario: Diagnostics are redacted
- **WHEN** proxy planning, an error, or transcript rendering displays commands
  or provider configuration
- **THEN** the gateway key value is absent and any secret field is represented
  by a fixed redaction marker

### Requirement: Proxy URL uses host loopback and runtime-safe forwarding

The proxy URL SHALL use HTTP, a loopback host, and an explicit port; wildcard
and non-loopback hosts SHALL be rejected. The documented setup SHALL use
`http://127.0.0.1:4000/v1`. The agent VM SHALL reach that endpoint through the
verified runtime-specific local-service forwarding mechanism under a dedicated
internal hostname while preserving port and path.

#### Scenario: Referenced LLM endpoint is accepted
- **WHEN** the user supplies `http://127.0.0.1:4000/v1`
- **THEN** the in-container provider URL targets port 4000 and retains `/v1`

#### Scenario: MCP or unsafe endpoint is not silently substituted
- **WHEN** the user supplies port 3000, `0.0.0.0`, or a non-loopback host
- **THEN** the CLI never rewrites it to the documented LLM endpoint; unsafe
  hosts are rejected and endpoint/API validation fails clearly for a non-LLM
  listener

#### Scenario: Runtime has no safe forwarding path
- **WHEN** no verified native or host-bindable forwarding strategy exists
- **THEN** startup fails before any container and never widens the proxy bind

### Requirement: Preflight discovers models and validates selection

Before starting forwarding resources or containers, the system SHALL perform
a bounded authenticated `GET <proxy-base>/models` request using the gateway
bearer key. It SHALL require a non-empty valid model-list response, validate
every non-empty string in `data[].id` without changing it, deduplicate exact IDs
while preserving response order, and use that complete catalog to configure the
selected agent. It SHALL validate
the regular `--model` selection against the catalog and SHALL NOT mutate or
manage the proxy.

#### Scenario: Proxy is unavailable
- **WHEN** the request cannot connect or times out
- **THEN** the CLI aborts with guidance to start or troubleshoot the external
  gateway and no container is started

#### Scenario: Gateway key is rejected
- **WHEN** the endpoint returns 401 or 403
- **THEN** the CLI aborts with gateway-key guidance without exposing the key

#### Scenario: Catalog is malformed or empty
- **WHEN** `/v1/models` does not contain a non-empty list of valid model IDs
- **THEN** the CLI rejects the endpoint before generating agent configuration

#### Scenario: Selected model is absent
- **WHEN** `/v1/models` succeeds but omits the model selected by `--model`
- **THEN** the CLI names the unavailable selection and aborts before container
  work

### Requirement: Firewall access is scoped to the forwarded proxy endpoint

With the firewall enabled, proxy mode SHALL add a TCP allow rule scoped to the
forwarded proxy address and port and leave all other firewall behavior
unchanged. Without proxy mode no such rule SHALL appear. With `--no-firewall`,
the CLI SHALL emit the same class of hostname-resolution warning used by local
Ollama support.

#### Scenario: Proxy rule is added
- **WHEN** proxy mode runs with the firewall enabled
- **THEN** the proxy endpoint and only its configured TCP port are added to the
  equivalent session's existing rules

#### Scenario: Non-proxy firewall is unchanged
- **WHEN** proxy mode is absent
- **THEN** rendered rules contain no proxy endpoint or hostname

### Requirement: Pi and OpenCode receive authenticated custom providers

Pi and OpenCode SHALL receive private, agent-specific provider configuration
containing the forwarded `/v1` base URL, every model discovered from the
endpoint, and the gateway key. Model selection SHALL use only the existing
`--model` dispatch; no proxy-specific model-list flag or discovery-order default
SHALL be introduced. No unsupported agent SHALL receive this provider or key.

#### Scenario: Pi provider is generated
- **WHEN** pi runs in proxy mode
- **THEN** `models.json` contains every discovered model, `settings.json`
  selects the authenticated proxy provider without a default model, and
  `--model <model-id>` selects the run model

#### Scenario: OpenCode provider is generated
- **WHEN** OpenCode runs in proxy mode
- **THEN** `opencode.json` defines provider `agent-proxy` with every discovered
  model and `--model agent-proxy/<model-id>` selects the run model

### Requirement: Dry run is complete, redacted, and side-effect free

Dry-run SHALL validate non-secret proxy arguments and the shape of the regular
`--model` selection, then preview sanitized agent, forwarding, firewall, and
container plans with discovery/provider generation marked as deferred. It
SHALL NOT read the gateway-key environment variable, contact the proxy, write
files, or start a resource.

#### Scenario: Proxy dry-run
- **WHEN** a valid proxy invocation includes `--dry-run`
- **THEN** output contains the URL and selected regular `--model`, states that
  catalog discovery is deferred, omits the gateway key, and performs no secret
  read, network request, filesystem write, or process start

### Requirement: Users can verify both supported agents end to end

The repository SHALL provide a stdlib-only executable checker that obtains the
gateway key from an `agentgateway-locally` checkout, discovers the authenticated
model catalog, and runs one real headless pi session and one real headless
OpenCode session through the proxy in an isolated temporary project. Each run
SHALL select its model through the regular `--model` option.

#### Scenario: Both agents work
- **WHEN** the proxy is up, authentication and selected models are valid, and
  each headless agent returns its unique expected marker
- **THEN** the checker reports both agents passed and exits zero

#### Scenario: Proxy precheck fails
- **WHEN** the proxy is down, rejects the key, returns malformed/empty model
  data, or lacks a selected model
- **THEN** the checker fails before launching either agent with an actionable,
  redacted error

#### Scenario: One agent fails
- **WHEN** pi or OpenCode exits nonzero, times out, or omits its expected marker
- **THEN** the checker reports the failing agent and exits nonzero

#### Scenario: Checker contains its side effects
- **WHEN** the checker finishes or is interrupted
- **THEN** its temporary project is removed, the gateway remains running and
  unmodified, and the gateway key has not appeared in argv or output
