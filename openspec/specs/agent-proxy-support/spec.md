## Purpose

Route Pi, OpenCode, or Bash LLM traffic through an authenticated local HTTP or remote HTTP/HTTPS gateway, keeping provider API keys and host OAuth credentials out of the sandbox while admitting only the gateway bearer key.

## Requirements

### Requirement: Proxy mode is opt-in and limited to supported agents

The CLI SHALL accept `--agent-proxy URL`, `--agent-proxy-key-env NAME`, and raw
`--agent-proxy-key KEY`, and SHALL reuse the regular `--model` option. Proxy
mode SHALL be accepted only for pi, OpenCode, and Bash and SHALL require `--model`.
When absent, all existing behavior SHALL remain unchanged.

#### Scenario: Supported agent opts in
- **WHEN** the user selects pi, OpenCode, or Bash with proxy URL
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
- **WHEN** proxy mode is requested with Claude, Codex, or another agent
- **THEN** the CLI exits before container work and names pi, OpenCode, and Bash as the
  supported agents

#### Scenario: Existing behavior is unchanged
- **WHEN** no proxy URL is supplied
- **THEN** no proxy preflight, forwarding, firewall rule, credential handling,
  or provider configuration occurs

### Requirement: Provider credentials are excluded while the gateway key is admitted

Proxy mode SHALL force `--no-forward-credentials` behavior and reject
`--api-key-env` and `--api-key-env-file`. No provider API key or host agent
OAuth credential SHALL enter the agent VM. The single gateway bearer key
resolved from the approved source precedence SHALL be the sole LLM credential
admitted by proxy mode and SHALL be staged only for the selected agent.

#### Scenario: Provider credentials are excluded
- **WHEN** proxy mode prepares a session
- **THEN** stale staged credentials are purged and no OpenAI/Anthropic key,
  host agent credential file, or unrelated credential mount is included

#### Scenario: Gateway key is present where required
- **WHEN** a supported agent is configured for an authenticated proxy
- **THEN** Pi/OpenCode private provider configuration or the selected Bash
  session environment can authenticate with the gateway key, and no unrelated
  agent profile or process receives that key

#### Scenario: Generic credential injection conflicts
- **WHEN** proxy mode is combined with `--api-key-env` or
  `--api-key-env-file`
- **THEN** the CLI rejects the invocation before reading or staging secrets

### Requirement: Gateway key sources are resolved in a fixed order
On real runs the gateway key SHALL be resolved in this order: captured output from `pass show agentgateway-api-key`; the host environment variable named by `--agent-proxy-key-env`, defaulting to `AGENTGATEWAY_API_KEY`; then raw `--agent-proxy-key`. The system SHALL require no access to an external `agentgateway-locally` checkout. It SHALL fail if all three sources are empty or unavailable.

#### Scenario: Pass entry succeeds
- **WHEN** `pass show agentgateway-api-key` returns a non-empty key
- **THEN** the CLI uses it without reading either fallback

#### Scenario: Environment fallback succeeds
- **WHEN** pass lookup fails or is empty and the selected environment variable
  contains a key
- **THEN** the CLI uses the environment value without reading the raw CLI
  fallback

#### Scenario: All gateway key sources fail
- **WHEN** pass, the selected environment variable, and the raw CLI fallback
  are all unavailable or empty
- **THEN** the CLI exits before network or container work with an error naming
  the attempted source types but no secret value

### Requirement: Gateway key input and output handling is secret-safe
The system SHALL NOT expose the resolved gateway key in its logs, errors, dry-run output, or transcripts, and SHALL protect and clean secret-bearing staged files using the existing private credential lifecycle. When the raw CLI fallback is used, the system SHALL warn that the invoking shell, history, and process listings may already expose argv and cannot be redacted retroactively.

#### Scenario: Raw CLI fallback succeeds with warning
- **WHEN** pass and environment lookup fail and `--agent-proxy-key` is present
- **THEN** the CLI uses it, warns about argv/history exposure without echoing
  the key, and redacts the value from all subsequent project-sandbox output

#### Scenario: Diagnostics are redacted
- **WHEN** proxy planning, an error, or transcript rendering displays commands
  or provider configuration
- **THEN** the gateway key value is absent and any secret field is represented
  by a fixed redaction marker

### Requirement: Proxy URL uses host loopback and runtime-safe forwarding

The proxy URL SHALL follow the shared endpoint contract: HTTP loopback via runtime-safe forwarding, or remote HTTP/HTTPS via direct sandbox access. Loopback HTTPS and invalid authorities SHALL be rejected. Omitted ports SHALL use 80/443. Local services SHALL share `host.docker.internal`; remote endpoints SHALL retain their configured hostname and scheme. Port and API path SHALL be preserved. The documented local example SHALL remain `http://127.0.0.1:4000/v1`.

#### Scenario: Referenced LLM endpoint is accepted
- **WHEN** the user supplies `http://127.0.0.1:4000/v1`
- **THEN** the in-container provider URL targets `host.docker.internal` on port 4000 and retains `/v1`

#### Scenario: MCP or unsafe endpoint is not silently substituted
- **WHEN** the user supplies port 3000, `0.0.0.0`, or an invalid remote endpoint
- **THEN** the CLI never rewrites it to the documented LLM endpoint; unsafe hosts are rejected and endpoint/API validation fails clearly for a non-LLM listener

#### Scenario: Runtime has no safe forwarding path
- **WHEN** no verified native or host-bindable forwarding strategy exists
- **THEN** startup fails before any container and never widens the proxy bind

#### Scenario: Apple localhost DNS setup is administrator-managed
- **WHEN** Apple `container` is selected for proxy forwarding
- **THEN** the provider uses `host.docker.internal`, the CLI prints the exact `sudo container system dns create host.docker.internal --localhost 203.0.113.113` setup command, warns that the change can disrupt container Internet access and might disable Private Relay, and requires restarting the container system afterward

#### Scenario: Container runtime uses one shared hostname mapping
- **WHEN** Docker Desktop, compatible Podman, a supported Linux bridge runtime, or chroot forwards one or more local services
- **THEN** `host.docker.internal` is mapped once to the runtime-selected host endpoint and Agentgateway remains restricted to its configured port; chroot forwarding applies only when Internet-proxy mode is absent

#### Scenario: Remote gateway retains its TLS identity and API path
- **WHEN** `--agent-proxy https://models.corp.example/v1` is configured
- **THEN** discovery and sandbox provider configuration use that hostname and `/v1` path on effective port 443 without host-loopback forwarding

### Requirement: Preflight discovers models and validates selection
Before starting forwarding resources or containers, the system SHALL perform a bounded direct authenticated `GET <proxy-base>/models` request using the gateway bearer key, and SHALL validate the regular `--model` selection against the discovered catalog. It SHALL NOT mutate or manage the proxy.

#### Scenario: Proxy is unavailable
- **WHEN** the request cannot connect or times out
- **THEN** the CLI aborts with guidance to start or troubleshoot the external
  gateway and no container is started

#### Scenario: Gateway key is rejected
- **WHEN** the endpoint returns 401 or 403
- **THEN** the CLI aborts with gateway-key guidance without exposing the key

#### Scenario: Selected model is absent
- **WHEN** `/v1/models` succeeds but omits the model selected by `--model`
- **THEN** the CLI names the unavailable selection and aborts before container
  work

#### Scenario: Discovery uses verified TLS and configured trust
- **WHEN** the gateway uses HTTPS with public trust or a CA supplied by `--ca-cert`
- **THEN** discovery verifies the endpoint certificate and hostname using that trust before transmitting the bearer key

#### Scenario: Host proxy environment or redirect attempts to change the route
- **WHEN** ambient host proxy variables are set or discovery receives a redirect
- **THEN** discovery does not use the ambient proxy or follow the redirect and does not send the bearer key to another origin

### Requirement: The discovered model catalog is validated and kept intact
The system SHALL require a non-empty valid model-list response, validate every non-empty string in `data[].id` without changing it, deduplicate exact IDs while preserving response order, and use that complete catalog to configure the selected agent.

#### Scenario: Catalog is malformed or empty
- **WHEN** `/v1/models` does not contain a non-empty list of valid model IDs
- **THEN** the CLI rejects the endpoint before generating agent configuration

### Requirement: Proxy firewall defaults to gateway-only egress
With the firewall enabled, proxy mode SHALL add a TCP allow rule scoped to the configured proxy endpoint address(es) and port, omit the normal provider-domain allowlist and the devcontainer's broad host-gateway exception, and retain only domains the user explicitly adds with `--extra-domain` or `--allow-github`. It SHALL NOT infer domain exceptions from a Bash prompt. Without proxy mode no such rule SHALL appear.

#### Scenario: Proxy rule is added
- **WHEN** proxy mode runs with the firewall enabled
- **THEN** the proxy endpoint and only its configured TCP port are added to the
  equivalent session's existing rules and reachability is checked after the
  final default-deny and reject rules are installed

#### Scenario: Default provider endpoints are excluded
- **WHEN** proxy mode runs without explicit domain options
- **THEN** OpenAI, Anthropic, GitHub, and the broad host gateway are not
  allowlisted, so LLM traffic can leave only through the scoped gateway rule

#### Scenario: Explicit domain additions are retained
- **WHEN** agent-proxy-only mode includes `--extra-domain example.com` or `--allow-github`
- **THEN** those requested destinations are added without restoring any other
  default provider endpoint

#### Scenario: Non-proxy firewall is unchanged
- **WHEN** proxy mode is absent
- **THEN** rendered rules contain no proxy endpoint or hostname

#### Scenario: Final remote gateway check verifies the real route
- **WHEN** a remote gateway passed host discovery but fails the post-firewall sandbox connection or TLS check
- **THEN** startup fails before agent execution without opening direct provider access or suggesting local forwarding setup

### Requirement: Agent proxy without the firewall or with an Internet proxy
With `--no-firewall`, remote endpoints SHALL use ordinary resolution without pinning guarantees; forwarded local endpoints SHALL retain the hostname-resolution warning used by local Ollama support. When an Internet proxy is also configured, its stricter option conflicts and endpoint-only egress policy SHALL apply.

#### Scenario: Remote gateway without the firewall
- **WHEN** an agent-proxy session with a remote endpoint runs with `--no-firewall`
- **THEN** the endpoint is resolved with ordinary resolution and no pinning is applied or claimed

#### Scenario: Local gateway without the firewall
- **WHEN** an agent-proxy session with a forwarded local endpoint runs with `--no-firewall`
- **THEN** the CLI prints the same hostname-resolution warning as local Ollama support

#### Scenario: Combined with an Internet proxy
- **WHEN** `--agent-proxy` and `--internet-proxy` are both given
- **THEN** the Internet proxy's option conflicts and endpoint-only egress policy apply

### Requirement: Supported agents receive authenticated proxy configuration
Pi and OpenCode SHALL receive private, agent-specific provider configuration containing the effective sandbox base URL with its configured API path, every model discovered from the endpoint, and the gateway key. Model selection SHALL use only the existing `--model` dispatch; no proxy-specific model-list flag or discovery-order default SHALL be introduced. No unsupported agent SHALL receive this provider or key.

#### Scenario: Pi provider is generated
- **WHEN** pi runs in proxy mode
- **THEN** `models.json` contains every discovered model, `settings.json`
  selects the authenticated proxy provider without a default model, and
  `--model <model-id>` selects the run model

#### Scenario: OpenCode provider is generated
- **WHEN** OpenCode runs in proxy mode
- **THEN** `opencode.json` defines provider `agent-proxy` with every discovered
  model and `--model agent-proxy/<model-id>` selects the run model

### Requirement: Bash sessions receive proxy environment and agent configs
For interactive and headless Bash sessions, the equivalent configuration SHALL be supplied as `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. Bash sessions SHALL additionally receive both private Pi and OpenCode proxy configs, including the effective sandbox URL, discovered catalog, gateway key, and selected default model.

#### Scenario: Interactive Bash receives proxy environment
- **WHEN** Bash runs interactively in proxy mode
- **THEN** the shell receives the effective sandbox URL, gateway key, and selected model
  as `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`

#### Scenario: Headless Bash receives proxy environment
- **WHEN** Bash runs with `--prompt` or `--prompt-text` in proxy mode
- **THEN** the executed command inherits the same three variables, with the key
  absent from process argv, logs, and transcripts

#### Scenario: Bash can launch pre-configured Pi and OpenCode
- **WHEN** interactive or headless Bash starts in proxy mode with a selected
  model
- **THEN** Pi defaults to provider `agent-proxy` and that model, OpenCode
  defaults to `agent-proxy/<model>`, and both mounted configs contain the
  forwarded catalog and gateway key

### Requirement: Dry run is complete, redacted, and side-effect free

Dry-run SHALL validate non-secret proxy arguments and the shape of the regular
`--model` selection, then preview sanitized agent, forwarding, firewall, and
container plans with discovery/provider generation marked as deferred. It
SHALL NOT read the gateway-key environment variable, contact the proxy, write
files, or start a resource, and SHALL NOT invoke pass. A supplied raw CLI key
SHALL be redacted from the preview.

#### Scenario: Proxy dry-run
- **WHEN** a valid proxy invocation includes `--dry-run`
- **THEN** output contains the URL and selected regular `--model`, states that
  catalog discovery is deferred, omits the gateway key, and performs no secret
  read, network request, filesystem write, or process start

### Requirement: Users can verify both supported agents end to end

The repository SHALL provide a stdlib-only executable checker that requires no
external checkout, resolves the gateway key from pass with environment and raw
CLI fallbacks, discovers the authenticated model catalog, and runs one real
headless pi session and one real headless OpenCode session through the proxy in
an isolated temporary project. Each run SHALL select its model through the
regular `--model` option.

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
  unmodified, the key is not repeated in child argv or emitted in output, and
  any raw-key input received the unavoidable parent-argv exposure warning

### Requirement: Agentgateway remains independent from Internet proxy routing

When agent and Internet proxies are enabled together, the gateway SHALL connect directly using its effective sandbox URL and bypass general proxy routing. Both `NO_PROXY` and `no_proxy` SHALL include its remote hostname or the shared local alias as appropriate. Its firewall rule SHALL remain scoped to its own endpoint address(es) and port. AI API and MCP routing SHALL NOT be implicitly redirected through the Internet proxy.

#### Scenario: Both proxy modes are enabled
- **WHEN** a supported agent starts with both `--agent-proxy` and `--internet-proxy`
- **THEN** Agentgateway traffic bypasses the Internet proxy and both services retain independent address/port firewall destinations, sharing the internal hostname only when both use loopback forwarding

#### Scenario: Filtering proxy fails independently
- **WHEN** the Internet proxy becomes unavailable while Agentgateway remains running
- **THEN** Agentgateway traffic remains reachable under the firewall
