## MODIFIED Requirements

### Requirement: Firewall enforcement and external policy ownership are mandatory
Internet proxy mode SHALL require the firewall and SHALL reject `--no-firewall`. In firewall egress mode (the default), Internet proxy mode SHALL also reject `--extra-domain` and `--allow-github`, and conflict errors SHALL explain that environment variables are bypassable without firewall enforcement and that Internet destination policy belongs in the external proxy. In sidecar egress mode (`--egress sidecar`), the external proxy SHALL act as the sidecar's upstream parent, and `--extra-domain` and `--egress-policy` SHALL be accepted because the sidecar enforces local policy in addition to the external proxy's policy. `--allow-github` stays rejected in sidecar mode, which blocks GitHub repository access.

#### Scenario: Firewall is disabled
- **WHEN** `--internet-proxy` is combined with `--no-firewall`
- **THEN** the CLI rejects the invocation before network or container work and explains the bypass risk

#### Scenario: Project-level Internet policy is requested
- **WHEN** `--internet-proxy` is combined with `--extra-domain` or `--allow-github` without `--egress sidecar`
- **THEN** the CLI rejects the invocation and directs the user to configure equivalent policy in the external filtering proxy, or to use `--egress sidecar` for local host policy

#### Scenario: Local policy in front of an upstream proxy
- **WHEN** `--internet-proxy https://proxy.corp.example` is combined with `--egress sidecar` and `--extra-domain pypi.org`
- **THEN** the CLI accepts the invocation, the sandbox's proxy variables name the sidecar, and the sidecar forwards allowed requests to `proxy.corp.example`
