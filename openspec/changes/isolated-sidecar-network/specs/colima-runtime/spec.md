# Spec Delta

## MODIFIED Requirements

### Requirement: Host-dependent features are unsupported on Colima
In firewall mode, the CLI SHALL reject, before any container work, a Colima launch that requires a path from the sandbox to the macOS host: an enabled local Ollama service or `--agent-proxy` to a macOS-local gateway. In sidecar mode these SHALL be supported through the sidecar's declared services (`sidecar-local-services`). In every mode, an upstream parent proxy listening on host loopback SHALL be rejected. Each error SHALL name the feature and say that it is unsupported on Colima, naming sidecar mode where it would work. An upstream parent proxy reachable by network address from the VM SHALL remain supported, and no rejection SHALL be bypassed by widening sandbox networking.

#### Scenario: Ollama is enabled
- **WHEN** a Colima firewall-mode launch has local Ollama enabled
- **THEN** the CLI exits before any container work, names Ollama as unsupported on Colima in firewall mode, and names `--egress sidecar`

#### Scenario: Ollama is enabled in sidecar mode
- **WHEN** a Colima sidecar-mode launch has local Ollama enabled and `ollama` is declared on port 11434
- **THEN** the sandbox reaches the Mac's loopback Ollama at `egress:11434` through the sidecar, and the sandbox's network is not widened

#### Scenario: Loopback parent proxy
- **WHEN** `egress.toml` sets `[egress.upstream] url = "http://127.0.0.1:3128"` and a Colima sidecar-mode launch starts
- **THEN** the CLI exits before the sidecar starts, names the loopback parent as unsupported on Colima, and suggests an address reachable from the VM

#### Scenario: Network-reachable parent proxy
- **WHEN** `egress.toml` sets `[egress.upstream] url = "https://proxy.corp.example"`
- **THEN** the sidecar starts with that parent and sidecar-mode sandboxes reach it through the sidecar
