# Spec Delta

## MODIFIED Requirements

### Requirement: Firewall enforcement and external policy ownership are mandatory
Internet proxy mode SHALL require the firewall and SHALL reject `--no-firewall`. In firewall egress mode (the default), Internet proxy mode SHALL also reject `--extra-domain` and `--allow-github`, and conflict errors SHALL explain that environment variables are bypassable without firewall enforcement and that Internet destination policy belongs in the external proxy. In sidecar egress mode (`--egress sidecar`), `--internet-proxy` SHALL be rejected at launch. An external proxy SHALL instead be configured as the global sidecar's upstream parent in `egress.toml`, where the sidecar enforces local hostname policy in front of it.

#### Scenario: Firewall is disabled
- **WHEN** `--internet-proxy` is combined with `--no-firewall`
- **THEN** the CLI rejects the invocation before network or container work and explains the bypass risk

#### Scenario: Project-level Internet policy is requested
- **WHEN** `--internet-proxy` is combined with `--extra-domain` or `--allow-github` without `--egress sidecar`
- **THEN** the CLI rejects the invocation and directs the user to configure equivalent policy in the external filtering proxy, or to use `--egress sidecar` for local host policy

#### Scenario: Internet proxy in sidecar mode
- **WHEN** `--internet-proxy https://proxy.corp.example` is combined with `--egress sidecar`
- **THEN** the CLI rejects the invocation before network or container work and names `[egress.upstream] url` in `egress.toml`

#### Scenario: Local policy in front of an upstream proxy
- **WHEN** `egress.toml` sets `[egress.upstream] url = "https://proxy.corp.example"` and allows `pypi.org`, and a sidecar-mode sandbox requests `pypi.org`
- **THEN** the sandbox's proxy variables name the sidecar, and the sidecar forwards the allowed request to `proxy.corp.example`
