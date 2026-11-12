# Spec Delta

## MODIFIED Requirements

### Requirement: Pi provider allowlist warning
When Pi is selected and the firewall is enabled, the system SHALL warn that only Pi's default-allowlisted providers are reachable, and that BYOK providers outside that set require `--extra-domain`, using the same generalized warning mechanism as OpenCode.

#### Scenario: Pi selected with firewall enabled
- **WHEN** the user selects `--agent pi` and the firewall is not disabled
- **THEN** the CLI prints a provider-allowlist warning equivalent to the existing OpenCode warning, worded for Pi, naming `--extra-domain` and not `--allow-github`

#### Scenario: Pi selected with firewall disabled
- **WHEN** the user selects `--agent pi` and passes the flag that disables the firewall
- **THEN** no provider-allowlist warning is printed
