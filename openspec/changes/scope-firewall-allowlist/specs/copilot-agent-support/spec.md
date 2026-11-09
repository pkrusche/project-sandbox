# Spec Delta

## ADDED Requirements

### Requirement: Copilot requires sidecar egress
`--agent copilot` SHALL be accepted only with `--egress sidecar`. In firewall mode, the CLI SHALL refuse it before any token lookup or container start, explaining that only the sidecar's Copilot profile limits GitHub access to the Copilot API. `--copilot-host` with a GHE.com tenant SHALL be refused until the sidecar supports tenant hosts. The system SHALL NOT enable GitHub access based on the content of a bash prompt.

#### Scenario: Copilot in firewall mode
- **WHEN** the user runs `--agent copilot` without `--egress sidecar`
- **THEN** the CLI exits with an error naming `--egress sidecar` and the `[egress.copilot]` setting, and no token is resolved

#### Scenario: Copilot in sidecar mode
- **WHEN** the user runs `--agent copilot --egress sidecar` with the Copilot profile enabled
- **THEN** the session starts, and the sandbox's GitHub access is limited to the sidecar's Copilot profile

#### Scenario: Tenant host
- **WHEN** the user runs `--agent copilot --copilot-host acme.ghe.com`
- **THEN** the CLI exits with an error stating that tenant hosts are not yet supported

#### Scenario: Bash prompt invoking copilot
- **WHEN** the user runs `--agent bash --prompt-text 'copilot -p hi'` in firewall mode
- **THEN** no GitHub host is admitted

## REMOVED Requirements

### Requirement: Copilot implies GitHub network access, chosen explicitly
**Reason**: Firewall mode can only admit `api.github.com` as a whole, which exposes the full GitHub API to the Copilot token. Sandboxes don't need GitHub repository access, and the sidecar's Copilot profile already limits access to the Copilot API paths.
**Migration**: Use `--agent copilot --egress sidecar`, with `[egress.copilot] enabled = true` in `egress.toml`. Hosts that are genuinely needed can be added with `--extra-domain`.
