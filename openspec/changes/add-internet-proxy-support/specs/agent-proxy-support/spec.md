## ADDED Requirements

### Requirement: Agentgateway remains independent from Internet proxy routing
When Agentgateway and Internet proxy modes are enabled together, the system SHALL forward Agentgateway through the shared `host.docker.internal` hostname, include that hostname once in both `NO_PROXY` and `no_proxy`, and retain its independently port-scoped firewall rule. AI API and MCP routing SHALL not be implicitly redirected through the general Internet proxy.

#### Scenario: Both proxy modes are enabled
- **WHEN** a supported agent starts with both `--agent-proxy` and `--internet-proxy`
- **THEN** Agentgateway traffic bypasses the Internet proxy and both services share one hostname while retaining distinct port-scoped firewall destinations

#### Scenario: Filtering proxy fails independently
- **WHEN** the Internet proxy becomes unavailable while Agentgateway remains running
- **THEN** Agentgateway traffic remains reachable under the firewall

## MODIFIED Requirements

### Requirement: Proxy URL uses host loopback and runtime-safe forwarding

The proxy URL SHALL use HTTP, a loopback host, and an explicit port; wildcard and non-loopback hosts SHALL be rejected. The documented setup SHALL use `http://127.0.0.1:4000/v1`. The agent VM SHALL reach that endpoint through the verified runtime-specific local-service forwarding mechanism using the shared internal hostname `host.docker.internal` while preserving port and path. Ollama, Agentgateway, and the Internet proxy SHALL use this same hostname and remain distinguishable by port.

#### Scenario: Referenced LLM endpoint is accepted
- **WHEN** the user supplies `http://127.0.0.1:4000/v1`
- **THEN** the in-container provider URL targets `host.docker.internal` on port 4000 and retains `/v1`

#### Scenario: MCP or unsafe endpoint is not silently substituted
- **WHEN** the user supplies port 3000, `0.0.0.0`, or a non-loopback host
- **THEN** the CLI never rewrites it to the documented LLM endpoint; unsafe hosts are rejected and endpoint/API validation fails clearly for a non-LLM listener

#### Scenario: Runtime has no safe forwarding path
- **WHEN** no verified native or host-bindable forwarding strategy exists
- **THEN** startup fails before any container and never widens the proxy bind

#### Scenario: Apple localhost DNS setup is administrator-managed
- **WHEN** Apple `container` is selected for proxy forwarding
- **THEN** the provider uses `host.docker.internal`, the CLI prints the exact `sudo container system dns create host.docker.internal --localhost 203.0.113.113` setup command, and warns that the change might disable network connectivity and requires restarting the container system afterward

#### Scenario: Container runtime uses one shared hostname mapping
- **WHEN** Docker Desktop, compatible Podman, a supported Linux bridge runtime, or chroot forwards one or more local services
- **THEN** `host.docker.internal` is mapped once to the runtime-selected host endpoint and Agentgateway remains restricted to its configured port
