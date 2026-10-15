## Purpose

Route Pi, OpenCode, or Bash LLM traffic through an authenticated local HTTP or remote HTTP/HTTPS gateway, keeping provider API keys and host OAuth credentials out of the sandbox while admitting only the gateway bearer key.

## MODIFIED Requirements

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

Before starting forwarding resources or containers, the system SHALL perform
a bounded direct authenticated `GET <proxy-base>/models` request using the gateway
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

#### Scenario: Discovery uses verified TLS and configured trust
- **WHEN** the gateway uses HTTPS with public trust or a CA supplied by `--ca-cert`
- **THEN** discovery verifies the endpoint certificate and hostname using that trust before transmitting the bearer key

#### Scenario: Host proxy environment or redirect attempts to change the route
- **WHEN** ambient host proxy variables are set or discovery receives a redirect
- **THEN** discovery does not use the ambient proxy or follow the redirect and does not send the bearer key to another origin


### Requirement: Proxy firewall defaults to gateway-only egress

With the firewall enabled, proxy mode SHALL add a TCP allow rule scoped to the
configured proxy endpoint address(es) and port, omit the normal provider-domain allowlist and
the devcontainer's broad host-gateway exception, and retain only domains the
user explicitly adds with `--extra-domain` or `--allow-github`. It SHALL NOT
infer domain exceptions from a Bash prompt. Without proxy mode no such rule
SHALL appear. With `--no-firewall`, remote endpoints SHALL use ordinary resolution without pinning guarantees; forwarded local endpoints SHALL retain the hostname-resolution warning used by local Ollama support. When an Internet proxy is also configured, its stricter option conflicts and endpoint-only egress policy SHALL apply.

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


### Requirement: Supported agents receive authenticated proxy configuration

Pi and OpenCode SHALL receive private, agent-specific provider configuration
containing the effective sandbox base URL with its configured API path, every model discovered from the
endpoint, and the gateway key. Model selection SHALL use only the existing
`--model` dispatch; no proxy-specific model-list flag or discovery-order default
SHALL be introduced. No unsupported agent SHALL receive this provider or key.
For interactive and headless Bash sessions, the equivalent configuration SHALL
be supplied as `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. Bash
sessions SHALL additionally receive both private Pi and OpenCode proxy configs,
including the effective sandbox URL, discovered catalog, gateway key, and selected
default model.

#### Scenario: Pi provider is generated
- **WHEN** pi runs in proxy mode
- **THEN** `models.json` contains every discovered model, `settings.json`
  selects the authenticated proxy provider without a default model, and
  `--model <model-id>` selects the run model

#### Scenario: OpenCode provider is generated
- **WHEN** OpenCode runs in proxy mode
- **THEN** `opencode.json` defines provider `agent-proxy` with every discovered
  model and `--model agent-proxy/<model-id>` selects the run model

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

### Requirement: Agentgateway remains independent from Internet proxy routing

When agent and Internet proxies are enabled together, the gateway SHALL connect directly using its effective sandbox URL and bypass general proxy routing. Both `NO_PROXY` and `no_proxy` SHALL include its remote hostname or the shared local alias as appropriate. Its firewall rule SHALL remain scoped to its own endpoint address(es) and port. AI API and MCP routing SHALL NOT be implicitly redirected through the Internet proxy.

#### Scenario: Both proxy modes are enabled
- **WHEN** a supported agent starts with both `--agent-proxy` and `--internet-proxy`
- **THEN** Agentgateway traffic bypasses the Internet proxy and both services retain independent address/port firewall destinations, sharing the internal hostname only when both use loopback forwarding

#### Scenario: Filtering proxy fails independently
- **WHEN** the Internet proxy becomes unavailable while Agentgateway remains running
- **THEN** Agentgateway traffic remains reachable under the firewall
