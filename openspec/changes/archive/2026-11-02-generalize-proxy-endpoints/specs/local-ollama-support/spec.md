## MODIFIED Requirements

### Requirement: One host-loopback alias resolves to the Ollama endpoint
The system SHALL use the shared internal hostname `host.docker.internal` for Apple `container`, Docker, Podman, supported Linux bridge modes, and chroot when `--pi-ollama` is set. Across supported combinations, enabled host-loopback services SHALL share this one hostname and SHALL remain distinguishable by port. Remote proxies SHALL retain their own hostnames and SHALL NOT alter the Ollama alias. Chroot SHALL support Ollama only when Internet-proxy mode is absent. The system SHALL pin the verified runtime-selected address for the container lifetime where the runtime permits it.

#### Scenario: Container startup with Pi-Ollama enabled
- **WHEN** the container starts with `--pi-ollama` set and the firewall enabled
- **THEN** `host.docker.internal` resolves to the verified native or bridge-proxy endpoint and Ollama is allowed only on port 11434

#### Scenario: Multiple local services are enabled
- **WHEN** Ollama is enabled with a host-loopback Agentgateway or Internet proxy
- **THEN** the runtime creates or requires only one `host.docker.internal` mapping or Apple localhost DNS entry and applies separate port-scoped rules to each service

#### Scenario: Alias already mapped by the runtime
- **WHEN** the runtime has already mapped the alias into `/etc/hosts` (for example a Docker `--add-host` entry)
- **THEN** the firewall's DNS pin recognizes the existing entry for that same alias and does not append a duplicate

#### Scenario: No dynamic address exposed to the agent process
- **WHEN** the container starts with `--pi-ollama` set
- **THEN** no `OLLAMA_HOST` (or equivalent) environment variable is set, and Pi's provider configuration references `host.docker.internal` rather than a dynamically exposed address

### Requirement: Local Ollama remains independent from Internet proxy routing
When local Ollama and Internet proxy modes are enabled together, the system SHALL forward Ollama through the shared `host.docker.internal` hostname, include that hostname once in both `NO_PROXY` and `no_proxy`, and retain its independent TCP rule scoped to port 11434. Ollama traffic SHALL not be routed through the general Internet proxy.

#### Scenario: Ollama and Internet proxy are enabled
- **WHEN** Pi starts with `--pi-ollama` and `--internet-proxy`
- **THEN** Ollama bypasses the Internet proxy and remains restricted to its port-scoped firewall rule; the Internet proxy shares the alias only when it is a host-loopback service

#### Scenario: Internet proxy option is absent
- **WHEN** Pi starts with `--pi-ollama` without `--internet-proxy`
- **THEN** Ollama uses the shared hostname while its existing configuration, lifecycle, and port-scoped firewall behavior remain unchanged

#### Scenario: Remote Internet proxy coexists with local Ollama
- **WHEN** Pi uses local Ollama with a remote HTTPS Internet proxy
- **THEN** Ollama retains HTTP forwarding through `host.docker.internal:11434`, that alias is in both bypass lists, and the remote proxy keeps its distinct direct endpoint

