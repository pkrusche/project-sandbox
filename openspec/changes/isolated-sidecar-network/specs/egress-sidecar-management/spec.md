# Spec Delta

## ADDED Requirements

### Requirement: Service edits apply to the running sidecar without a restart
`service add NAME PORT`, `service remove NAME` and `service list` SHALL edit and show `[egress.services]`. Edits SHALL be validated like the policy file, SHALL keep comments and unrelated keys, and SHALL apply to a running isolated-strategy sidecar without restarting it. An invalid edit SHALL leave `egress.toml` unchanged.

#### Scenario: Add a service live
- **WHEN** an isolated sidecar is running and the user runs `project-sandbox-egress service add ollama 11434`
- **THEN** a running sandbox's next request to `http://egress:11434/` reaches the host's Ollama, and the sidecar wasn't restarted

#### Scenario: Remove a service
- **WHEN** the user runs `service remove ollama`
- **THEN** a sandbox's next connection to `egress:11434` is refused, and the command says that requests already in flight may complete

#### Scenario: Invalid edit
- **WHEN** the user runs `service add other 3128`
- **THEN** the command exits non-zero naming the reserved port, and `egress.toml` is byte-identical

#### Scenario: Published-endpoint runtime
- **WHEN** the user runs `service list --runtime docker`
- **THEN** it lists the declared services and says they're exposed only on the isolated strategy

## MODIFIED Requirements

### Requirement: Status reports state without changing it
`status` SHALL report the sidecar's state without creating, starting or removing anything: running and ready, the published endpoint (or, on the isolated strategy, the internal address and networks), the declared services, image currency, configuration drift against `egress.toml` (each field that needs a reload or a restart), CA constraints and expiry, relay state and the policy path. `status --json` SHALL emit the same as JSON.

#### Scenario: Stale configuration
- **WHEN** `egress.toml` gained an allowlist entry after the sidecar started
- **THEN** `status` reports that the allowlist differs and names `project-sandbox-egress reload`

#### Scenario: Port drift
- **WHEN** `egress.toml` sets a different port than the running sidecar publishes
- **THEN** `status` reports the port as needing a restart

#### Scenario: Sidecar absent
- **WHEN** no sidecar exists
- **THEN** `status` reports it as not running and exits zero

#### Scenario: Isolated strategy
- **WHEN** the sidecar runs on Apple `container` with `ollama` declared
- **THEN** `status` reports the sidecar's address on the host-only network, both networks, and `ollama` on port 11434, and reports no published endpoint
