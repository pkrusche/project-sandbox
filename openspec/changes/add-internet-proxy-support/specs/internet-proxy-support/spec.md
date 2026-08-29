## Purpose

Provide fail-closed, implementation-neutral routing of ordinary sandbox Internet traffic through a separately managed local filtering proxy while preserving independent local-service boundaries.

## ADDED Requirements

### Requirement: Internet proxy mode is explicitly configured and validated
The CLI SHALL accept `--internet-proxy URL` only when URL is HTTP, has an explicit valid port, uses `127.0.0.1`, `localhost`, or `::1`, contains no credentials, path, query, or fragment, and a supported container runtime is selected. It SHALL reject wildcard and non-loopback hosts. It SHALL reject chroot because its shared host network namespace cannot enforce an isolated sandbox firewall. It SHALL expose no implementation-specific proxy flags and SHALL leave all behavior unchanged when the option is absent.

#### Scenario: Valid loopback listener is accepted
- **WHEN** the user supplies `--internet-proxy http://127.0.0.1:18080`
- **THEN** the CLI accepts the listener and prepares Internet proxy mode

#### Scenario: Unsafe or ambiguous URL is rejected
- **WHEN** the URL uses a non-HTTP scheme, non-loopback or wildcard host, credentials, query, fragment, missing port, or invalid port
- **THEN** the CLI rejects it before network or container work with an actionable validation error

#### Scenario: Option is absent
- **WHEN** `--internet-proxy` is not supplied
- **THEN** no Internet-proxy validation, preflight, forwarding, environment, or firewall behavior occurs and existing output remains unchanged

#### Scenario: Chroot runtime is selected
- **WHEN** `--internet-proxy` is combined with `--runtime chroot`
- **THEN** the CLI rejects the invocation before network or sandbox work and directs the user to a supported container runtime

### Requirement: Firewall enforcement and external policy ownership are mandatory
Internet proxy mode SHALL require the firewall and SHALL reject `--no-firewall`, `--extra-domain`, and `--allow-github`. Conflict errors SHALL explain that environment variables are bypassable without firewall enforcement and that Internet destination policy belongs in `internet-proxy-locally`.

#### Scenario: Firewall is disabled
- **WHEN** `--internet-proxy` is combined with `--no-firewall`
- **THEN** the CLI rejects the invocation before network or container work and explains the bypass risk

#### Scenario: Project-level Internet policy is requested
- **WHEN** `--internet-proxy` is combined with `--extra-domain` or `--allow-github`
- **THEN** the CLI rejects the invocation and directs the user to configure equivalent policy in the external filtering proxy

### Requirement: Proxy forwarding is runtime-safe and implementation-neutral
The system SHALL forward the configured host-loopback listener under the shared internal hostname `host.docker.internal`, preserving its port. Ollama, Agentgateway, and the Internet proxy SHALL use that single hostname so Apple `container` requires only one administrator-managed localhost DNS entry and other supported container runtimes require only one host mapping. The system SHALL map it through Apple `container` localhost DNS, Docker Desktop and compatible Podman host-gateway forwarding, or supported Linux bridge forwarding with managed per-port loopback bridges where required. It SHALL fail closed when no safe forwarding strategy exists and SHALL NOT widen a loopback bind. Chroot forwarding SHALL remain available to local services outside Internet-proxy mode but is not a supported Internet-proxy strategy.

#### Scenario: Runtime forwarding is available
- **WHEN** the selected runtime has a verified strategy for a loopback service
- **THEN** `host.docker.internal` resolves or is pinned once to that strategy's endpoint for the sandbox lifetime and all enabled services use it with distinct ports

#### Scenario: Runtime forwarding is unavailable
- **WHEN** the runtime cannot safely forward and enforce access to the loopback service
- **THEN** startup fails before the sandbox starts and no wildcard listener is created

### Requirement: Standard proxy environment is injected consistently
Internet proxy mode SHALL set `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` to `http://host.docker.internal:<port>` for interactive, headless, and Bash sessions so generated agent processes inherit them unless an agent-specific launcher deliberately overrides a value. It SHALL set identical `NO_PROXY` and `no_proxy` lists containing `localhost`, `127.0.0.1`, `::1`, and `host.docker.internal` when an enabled local service such as Agentgateway or Ollama must bypass general proxy routing. Including the shared hostname SHALL NOT exempt ordinary Internet destinations from proxy routing.

#### Scenario: Internet proxy environment is generated
- **WHEN** Internet proxy mode prepares a session
- **THEN** upper- and lowercase routing variables contain the forwarded URL and upper- and lowercase bypass variables contain local loopback entries

#### Scenario: Other local services coexist
- **WHEN** Agentgateway, Ollama, or another explicitly supported local service is also forwarded
- **THEN** `host.docker.internal` appears once in both bypass lists and the local service's traffic is not routed through the Internet proxy

#### Scenario: Shared hostname does not bypass Internet destinations
- **WHEN** `host.docker.internal` is present in the bypass lists and is also the configured Internet proxy host
- **THEN** requests for ordinary Internet destinations still use the configured proxy because bypass matching applies to the requested destination

### Requirement: Firewall collapses ordinary Internet egress onto the proxy
With Internet proxy mode active, the firewall SHALL retain default DROP policies, loopback access, ESTABLISHED/RELATED handling, and blocked general outbound DNS. It SHALL allow new TCP connections only to the forwarded Internet-proxy address on its configured port plus separate port-scoped rules for explicitly enabled local services. It SHALL omit ordinary Internet destinations, GitHub ranges, AI-provider endpoints, extra domains, and broad host-gateway access from IPv4 and IPv6 allowlists. It SHALL enforce equivalent IPv6 denial or abort setup if the intended IPv6 policy cannot be enforced.

#### Scenario: Proxy-only egress policy is rendered
- **WHEN** Internet proxy mode runs with the firewall enabled
- **THEN** the rules include a TCP accept scoped to the proxy endpoint and port and retain default-deny and established-response behavior

#### Scenario: Direct public access is not admitted
- **WHEN** Internet proxy firewall rules are rendered
- **THEN** no GitHub, OpenAI, Anthropic, extra-domain, ordinary public IPv4, or ordinary public IPv6 destination is directly allowlisted and general DNS remains blocked

#### Scenario: IPv6 enforcement is unavailable
- **WHEN** the selected runtime cannot enforce the required IPv6 policy
- **THEN** firewall setup fails closed before the agent process starts

#### Scenario: Proxy-aware application bypasses routing hints
- **WHEN** a process unsets proxy variables or requests direct networking
- **THEN** direct Internet connection and DNS attempts fail under the firewall rather than falling back to unrestricted egress

### Requirement: Listener availability is preflighted without proxy management
Before a real sandbox starts, the system SHALL perform a bounded implementation-neutral TCP or HTTP listener preflight against the original host-loopback URL without requiring an allowed public destination. It SHALL fail actionably if unavailable, skip all network preflight during dry-run, and SHALL never start, stop, restart, build, pull, repair, configure, or test implementation-specific behavior of the external proxy.

#### Scenario: Listener is available
- **WHEN** a bounded connection to the configured loopback listener succeeds
- **THEN** sandbox preparation continues and a second TCP-only check through the final port-scoped container firewall verifies the forwarded path without inspecting the proxy implementation

#### Scenario: Listener is unavailable
- **WHEN** no listener responds before the bounded preflight ends
- **THEN** startup aborts before the sandbox starts and tells the user to start or troubleshoot the external proxy

#### Scenario: Apple localhost redirect requires rebuilt runtime networking
- **WHEN** the host listener is available but the forwarded TCP path is unavailable inside an Apple container
- **THEN** firewall initialization aborts and explains that the administrator-managed DNS domain must be configured before restarting the container system

#### Scenario: Dry-run previews proxy mode
- **WHEN** valid Internet proxy arguments include `--dry-run`
- **THEN** sanitized forwarded environment and firewall plans are shown without network access, file writes, container starts, or forwarding-resource creation

### Requirement: Proxy and gateway failures remain independent
If the Internet proxy becomes unavailable during a session, ordinary Internet operations SHALL fail and direct fallback SHALL remain blocked while an independently running Agentgateway remains reachable. If Agentgateway becomes unavailable, AI/MCP operations through it SHALL fail while ordinary Internet access through a running Internet proxy remains available. The system SHALL NOT restart either external service.

#### Scenario: Internet proxy stops
- **WHEN** the filtering proxy dies while Agentgateway remains available
- **THEN** general Internet requests fail, direct fallback stays blocked, and Agentgateway traffic continues

#### Scenario: Agentgateway stops
- **WHEN** Agentgateway dies while the filtering proxy remains available
- **THEN** AI/MCP requests through Agentgateway fail and permitted general Internet requests continue through the filtering proxy

### Requirement: The security boundary is documented
The repository SHALL document that project-sandbox iptables prevents bypass, `internet-proxy-locally` owns Internet destination/security policy, and `agentgateway-locally` owns AI/MCP routing and provider credential isolation. Documentation SHALL state that proxy environment variables are routing hints, non-proxy-aware applications fail rather than receive transparent interception, loopback publication alone is not the boundary, and Docker and Apple `container` are the primary supported runtimes. It SHALL contain no Docker Compose setup.

#### Scenario: User consults Internet proxy documentation
- **WHEN** the user reads `docs/internet-proxy.md`
- **THEN** setup responsibility, the three independent boundaries, enforcement limitations, failure behavior, primary runtimes, and the external policy repository are explicit

### Requirement: End-to-end filtering and bypass prevention are verifiable
The project SHALL define acceptance checks against the external proxy test setup that distinguish allowed and rejected HTTP/HTTPS policy, reject private, link-local, metadata, IPv4, and IPv6 destinations as configured, and prove that direct TCP, direct DNS, `curl --noproxy '*'`, and removal of proxy variables cannot bypass the firewall.

#### Scenario: Allowed and denied destinations are exercised
- **WHEN** acceptance checks run with a configured external filtering proxy
- **THEN** permitted HTTP/HTTPS requests succeed through it and prohibited public or private destinations are rejected according to external proxy policy

#### Scenario: Direct bypass is attempted
- **WHEN** acceptance checks use direct public IP connections, direct DNS, `--noproxy '*'`, or unset proxy variables
- **THEN** the sandbox firewall blocks each direct path
