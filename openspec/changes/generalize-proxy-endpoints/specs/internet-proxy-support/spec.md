## Purpose

Provide fail-closed, implementation-neutral routing of ordinary sandbox Internet traffic through a separately managed local HTTP or remote HTTP/HTTPS proxy while preserving independent gateway and local-service boundaries.

## MODIFIED Requirements

### Requirement: Internet proxy mode is explicitly configured and validated

The CLI SHALL accept `--internet-proxy URL` following the shared proxy-endpoint validation contract: HTTP for loopback and HTTP/HTTPS for remote endpoints, no userinfo/query/fragment, and no path other than empty or `/`. It SHALL reject chroot because its shared host network cannot enforce the sandbox firewall. No implementation-specific proxy flags SHALL be exposed. Absence of this option SHALL leave existing behavior unchanged.

#### Scenario: Valid loopback listener is accepted
- **WHEN** the user supplies `--internet-proxy http://127.0.0.1:18080`
- **THEN** the CLI accepts the listener and prepares Internet proxy mode

#### Scenario: Unsafe or ambiguous URL is rejected
- **WHEN** the URL uses a unsupported scheme, HTTPS loopback host, wildcard host, credentials, query, fragment, disallowed path, or invalid port
- **THEN** the CLI rejects it before network or container work with an actionable validation error

#### Scenario: Option is absent
- **WHEN** `--internet-proxy` is not supplied
- **THEN** no Internet-proxy validation, preflight, forwarding, environment, or firewall behavior occurs and existing output remains unchanged

#### Scenario: Chroot runtime is selected
- **WHEN** `--internet-proxy` is combined with `--runtime chroot`
- **THEN** the CLI rejects the invocation before network or sandbox work and directs the user to a supported container runtime

#### Scenario: Remote endpoint is accepted
- **WHEN** the user supplies `--internet-proxy https://proxy.corp.example`
- **THEN** the sandbox connects directly using that hostname and effective port 443 without local forwarding


### Requirement: Proxy forwarding is runtime-safe and implementation-neutral

Loopback Internet proxies SHALL use the existing runtime-safe `host.docker.internal` forwarding path with their configured port. Local Ollama and local agent gateways SHALL share that mapping with distinct ports. Remote proxies SHALL connect directly and retain their URL hostname. Forwarding SHALL never widen loopback binds; unavailable safe forwarding SHALL abort startup. Chroot SHALL remain unsupported for Internet proxy mode.

#### Scenario: Runtime forwarding is available
- **WHEN** the selected runtime has a verified strategy for a loopback service
- **THEN** `host.docker.internal` resolves or is pinned once to that strategy's endpoint for the sandbox lifetime and all enabled loopback services use it with distinct ports

#### Scenario: Runtime forwarding is unavailable
- **WHEN** the runtime cannot safely forward and enforce access to the loopback service
- **THEN** startup fails before the sandbox starts and no wildcard listener is created

#### Scenario: Remote-only Apple container session
- **WHEN** an Apple container session uses remote proxies and no local service
- **THEN** proxy setup neither requires a localhost DNS entry nor prints instructions to create one


### Requirement: Standard proxy environment is injected consistently

Internet proxy mode SHALL set `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` consistently to its effective sandbox URL in all supported launch modes. Identical `NO_PROXY` and `no_proxy` lists SHALL include loopback entries and configured direct agent-gateway hosts, plus `host.docker.internal` when local gateway or Ollama bypass is required. Values SHALL be deduplicated and SHALL NOT add wildcard or network-wide bypasses.

#### Scenario: Internet proxy environment is generated
- **WHEN** Internet proxy mode prepares a session
- **THEN** upper- and lowercase routing variables contain the effective sandbox URL, preserving HTTPS for a remote HTTPS proxy and upper- and lowercase bypass variables contain local loopback entries

#### Scenario: Other local services coexist
- **WHEN** Agentgateway, Ollama, or another explicitly supported local service is also forwarded
- **THEN** `host.docker.internal` appears once in both bypass lists and the local service's traffic is not routed through the Internet proxy

#### Scenario: Shared hostname does not bypass Internet destinations
- **WHEN** `host.docker.internal` is present in the bypass lists and is also the configured Internet proxy host
- **THEN** requests for ordinary Internet destinations still use the configured proxy because bypass matching applies to the requested destination

#### Scenario: Remote gateway bypasses the Internet proxy
- **WHEN** the gateway is `https://models.corp.example/v1` and an Internet proxy is enabled
- **THEN** both bypass lists include `models.corp.example` and generated gateway traffic connects directly

#### Scenario: Routing values cannot be replaced through generic injection
- **WHEN** generic environment injection supplies conflicting proxy variables
- **THEN** the generated policy-owned proxy and bypass values take precedence


### Requirement: Firewall collapses ordinary Internet egress onto the proxy

Internet proxy mode SHALL retain default DROP, loopback, ESTABLISHED/RELATED, and blocked general DNS. It SHALL allow new TCP connections only to each configured proxy endpoint and explicit local service at its own admitted addresses and port. No ordinary provider, GitHub, extra-domain, or broad gateway exceptions SHALL be added. IPv4/IPv6 policy SHALL be enforced; failure to enforce required IPv6 denial SHALL abort setup.

#### Scenario: Proxy-only egress policy is rendered
- **WHEN** Internet proxy mode runs with the firewall enabled
- **THEN** the rules include a TCP accept scoped to the proxy endpoint and port and retain default-deny and established-response behavior

#### Scenario: Direct public access is not admitted
- **WHEN** Internet proxy firewall rules are rendered
- **THEN** no GitHub, provider, extra-domain, or ordinary Internet destination is directly allowlisted beyond the exact configured service endpoints and general DNS remains blocked

#### Scenario: IPv6 enforcement is unavailable
- **WHEN** the selected runtime cannot enforce the required IPv6 policy
- **THEN** firewall setup fails closed before the agent process starts

#### Scenario: Proxy-aware application bypasses routing hints
- **WHEN** a process unsets proxy variables or requests direct networking
- **THEN** direct Internet connection and DNS attempts fail under the firewall rather than falling back to unrestricted egress

### Requirement: Listener availability is preflighted without proxy management

Real runs SHALL check the configured endpoint on the host and through the final sandbox firewall using bounded TCP for HTTP or verified TLS for HTTPS, without requiring a public destination. Failed checks SHALL abort before agent execution. Dry-run SHALL skip network work. The system SHALL NOT start, stop, restart, build, pull, repair, configure, or inspect implementation-specific behavior of the external proxy.

#### Scenario: Listener is available
- **WHEN** a bounded connection to the configured listener succeeds
- **THEN** sandbox preparation continues and a second transport-appropriate check through the final port-scoped container firewall verifies the actual sandbox path without inspecting the proxy implementation

#### Scenario: Listener is unavailable
- **WHEN** no listener responds before the bounded preflight ends
- **THEN** startup aborts before the sandbox starts and tells the user to start or troubleshoot the external proxy

#### Scenario: Apple localhost redirect requires rebuilt runtime networking
- **WHEN** the host listener is available but the forwarded TCP path is unavailable inside an Apple container
- **THEN** firewall initialization aborts and explains that the administrator-managed DNS domain must be configured before restarting the container system

#### Scenario: Dry-run previews proxy mode
- **WHEN** valid Internet proxy arguments include `--dry-run`
- **THEN** sanitized effective URLs, environment, and firewall plans are shown without network access, file writes, container starts, or forwarding-resource creation

### Requirement: The security boundary is documented

Documentation SHALL explain firewall bypass prevention, externally owned Internet policy, and separate gateway credential isolation. It SHALL cover independent and mixed local/remote proxy use, verified HTTPS, CA installation, session DNS pinning, tested client compatibility, and absent Internet-proxy authentication. Routing hints SHALL NOT be described as enforcement or transparent interception. Docker and Apple container SHALL remain primary runtimes; no Docker Compose setup SHALL be required.

#### Scenario: User consults Internet proxy documentation
- **WHEN** the user reads `docs/internet-proxy.md`
- **THEN** setup responsibility, the three independent boundaries, enforcement limitations, failure behavior, primary runtimes, and externally managed policy and network connectivity are explicit

## ADDED Requirements

### Requirement: Internet proxy operation is independent of agent proxy configuration
Internet proxy mode SHALL work without an agent proxy or gateway key and SHALL retain normal agent/provider credential handling in that case. It SHALL also coexist with an agent proxy or supported local Ollama configuration. Only enabling the agent proxy SHALL trigger gateway model discovery and gateway credential isolation.

#### Scenario: Internet proxy without a gateway
- **WHEN** an Internet proxy is enabled without `--agent-proxy`
- **THEN** no gateway key or model catalog is required and proxy-aware provider requests use the Internet proxy with ordinary provider credentials

#### Scenario: Both proxies are configured
- **WHEN** both proxy options are present
- **THEN** gateway traffic bypasses the Internet proxy and each endpoint keeps its own firewall permission and failure behavior

### Requirement: Internet proxy authentication is unsupported
The CLI SHALL reject Internet-proxy URL credentials and SHALL NOT provide proxy authentication flags, credential injection, client certificates, or authentication negotiation. Proxies requiring authentication are outside the supported scope of this version.

#### Scenario: Proxy requires authentication
- **WHEN** a configured Internet proxy returns HTTP 407 to a client request
- **THEN** the operation fails without acquiring credentials, forwarding the agent gateway key, or falling back to direct Internet access
