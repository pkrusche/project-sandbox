# Spec Delta

## Purpose

Lets Colima sidecar-mode sandboxes reach host-loopback HTTP services, such as local Ollama, a loopback agent gateway and later an MCP proxy, through the sidecar. These services are declared once in the global egress policy.

## ADDED Requirements

### Requirement: Local services are declared in the global policy
`egress.toml` SHALL accept an `[egress.services]` table mapping a service name to a TCP port on the host's loopback (1024–65535). Names SHALL be lowercase identifiers. Ports SHALL be unique, and SHALL NOT be the proxy's own port 3128. An invalid table SHALL fail validation, naming the key, before any container work.

#### Scenario: Valid declaration
- **WHEN** `egress.toml` declares `ollama = { port = 11434 }` and `agent-proxy = { port = 4000 }`
- **THEN** the policy loads, and both services are part of the effective policy

#### Scenario: Duplicate or reserved port
- **WHEN** two services declare port 4000, or one declares port 3128
- **THEN** loading the policy fails and names the offending key

### Requirement: The sidecar exposes each declared service on its port
In Colima sidecar mode, the sidecar SHALL serve each declared service as plain HTTP on `egress:<port>`, relaying every request to that port on the Mac's loopback through `host.lima.internal`. A sandbox SHALL reach the service without proxy environment variables. Services SHALL NOT be exposed on any other runtime.

#### Scenario: Ollama through the sidecar
- **WHEN** Ollama listens only on the Mac's `127.0.0.1:11434`, `ollama` is declared, and a Colima sidecar-mode sandbox requests `http://egress:11434/api/tags`
- **THEN** the request reaches the Mac's Ollama and the response is returned

#### Scenario: Host service down
- **WHEN** a declared service isn't listening on the Mac
- **THEN** the sandbox's request fails with an HTTP error from the sidecar, and other services and Internet traffic keep working

### Requirement: Service routes and forward-proxy rules are separate
A request on a service port SHALL go only to that service's host-loopback port, whatever its `Host` header, absolute URI or `CONNECT` request says. Forward-proxy requests on port 3128 SHALL still be denied for private, loopback, link-local and metadata destinations, including the Mac's address as seen from the VM.

#### Scenario: Pivot through a service port
- **WHEN** a sandbox sends `GET http://example.com/` or `CONNECT example.com:443` to `egress:11434`
- **THEN** the request is refused or served only by the Mac's port 11434, and no connection to `example.com` is made

#### Scenario: Host address through the forward proxy
- **WHEN** a sandbox asks `egress:3128` for `http://192.168.5.2:11434/` or `http://host.lima.internal:11434/`
- **THEN** the sidecar denies the request

#### Scenario: Undeclared host port
- **WHEN** a sandbox connects to `egress` on a port that is neither 3128 nor a declared service
- **THEN** the connection is refused

### Requirement: Service traffic is streamed
The sidecar SHALL relay service responses incrementally, without buffering a whole response, so streamed model output and server-sent events reach the sandbox as the host service produces them. Service requests SHALL NOT be cached.

#### Scenario: Token stream
- **WHEN** a sandbox sends a streaming generation request to Ollama through the sidecar
- **THEN** the first chunks arrive before the host service finishes the response

#### Scenario: Server-sent events
- **WHEN** a host service keeps a `text/event-stream` response open and sends an event every second
- **THEN** the sandbox receives each event within a second of it being sent

### Requirement: Launches that need a host service require its declaration
In Colima sidecar mode, a launch with `--pi-ollama` SHALL require a service on port 11434. A launch with `--agent-proxy` to a host-loopback URL SHALL require a service on that URL's port. Otherwise the launch SHALL fail before any container work, naming the `project-sandbox-egress service add` command to run.

#### Scenario: Ollama not declared
- **WHEN** a Colima sidecar-mode launch uses `--pi-ollama` and no service declares port 11434
- **THEN** the launch fails before any container work and prints `project-sandbox-egress service add ollama 11434`

#### Scenario: Agent proxy declared
- **WHEN** a Colima sidecar-mode launch uses `--agent-proxy http://127.0.0.1:4000/v1` and a service declares port 4000
- **THEN** the sandbox's agent proxy URL is `http://egress:4000/v1`

#### Scenario: Remote agent proxy
- **WHEN** a Colima sidecar-mode launch uses `--agent-proxy https://gateway.corp.example/v1`
- **THEN** no service declaration is required, the gateway's hostname must be allowed by the egress policy, and the agent reaches it through the forward proxy

### Requirement: Declared services are shared by every sandbox
Every Colima sidecar-mode sandbox on a profile SHALL be able to reach every declared service, whichever session's launch needed it. The Mac sees every relayed request as coming from its own loopback. The documentation SHALL say both, and SHALL name firewall mode as the choice for sessions that need per-session service access.

#### Scenario: Another session's service
- **WHEN** session A declared `agent-proxy` and session B, launched without `--agent-proxy`, requests `http://egress:4000/`
- **THEN** the request reaches the host service, whose own authentication applies

### Requirement: The sandbox environment points at the sidecar for services
In Colima sidecar mode, the sandbox's Ollama and agent proxy URLs SHALL use `egress` and the service port. `NO_PROXY`/`no_proxy` SHALL list `egress` so service traffic doesn't pass through the forward proxy. The environment SHALL NOT contain `host.docker.internal` or `host.lima.internal`.

#### Scenario: Pi with Ollama
- **WHEN** a Colima sidecar-mode session starts with `--pi-ollama`
- **THEN** Pi's Ollama base URL is `http://egress:11434/v1`, and `NO_PROXY` contains `egress`
