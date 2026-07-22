## MODIFIED Requirements

### Requirement: Pi credentials are mounted as a flat file
When credential forwarding is enabled, the system SHALL sync Pi's credential file (`~/.pi/agent/auth.json`, mode 0600) through the generic single-file credential path. It SHALL mount the staged file read-only at `/project-sandbox-secrets/pi` and copy it to `$HOME/.pi/agent/auth.json` only when Pi is the selected direct agent or the execution mode is intentionally multi-agent. It SHALL NOT mount a `/project-sandbox-config/pi` path solely for credential forwarding.

#### Scenario: Credentials present for a selected Pi run
- **WHEN** `~/.pi/agent/auth.json` exists on the host and Pi is the selected agent
- **THEN** the file is staged, bind-mounted read-only at `/project-sandbox-secrets/pi`, and copied to `$HOME/.pi/agent/auth.json` inside the container

#### Scenario: Pi credentials excluded from another named-agent run
- **WHEN** `~/.pi/agent/auth.json` exists on the host and Claude, Codex, or OpenCode is the selected agent
- **THEN** `/project-sandbox-secrets/pi` is not mounted and `$HOME/.pi/agent/auth.json` is not provisioned

#### Scenario: Pi credentials included in a bash session
- **WHEN** `~/.pi/agent/auth.json` exists on the host and bash is selected with credential forwarding enabled
- **THEN** the staged Pi credential is mounted and provisioned as part of the documented multi-agent environment

#### Scenario: Pi credentials included in a devcontainer
- **WHEN** `~/.pi/agent/auth.json` exists on the host and a devcontainer is generated with credential forwarding enabled
- **THEN** the staged Pi credential is included as part of the documented multi-agent devcontainer environment

#### Scenario: Credentials absent on host
- **WHEN** `~/.pi/agent/auth.json` does not exist on the host
- **THEN** no `/project-sandbox-secrets/pi` mount is created and no error is raised

#### Scenario: No credential-derived config mount
- **WHEN** Pi credentials are eligible for forwarding
- **THEN** credential forwarding does not create a `/project-sandbox-config/pi` mount or place `auth.json` in a generated configuration directory

