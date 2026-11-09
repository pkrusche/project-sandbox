# Spec Delta

## MODIFIED Requirements

### Requirement: Launches reconcile configuration drift
Before starting a sandbox, a sidecar-mode launch SHALL compare `egress.toml` and the sidecar image with what the running sidecar uses. Allowlist differences SHALL be applied to the running sidecar without a restart. Differences that need a restart SHALL produce a warning naming each field and `project-sandbox-egress restart`, and the launch SHALL continue on the running sidecar, except for the published port (see "The sidecar's proxy endpoint is published on the host"). These are the upstream, the Copilot profile, the port, the resource limits and the image. The policy compared and applied SHALL be the contents of `egress.toml` at the time the launch holds the sidecar lock, not a copy read earlier in the launch.

#### Scenario: Hand-edited allowlist
- **WHEN** the user added a host to `egress.toml` by hand while the sidecar was running
- **THEN** the next sidecar-mode launch applies it to the running sidecar without restarting it

#### Scenario: Upgraded image
- **WHEN** the running sidecar uses an older sidecar image than the installed version would build
- **THEN** the launch warns that the sidecar is stale and names `project-sandbox-egress restart`

#### Scenario: Host revoked during a launch
- **WHEN** a sidecar-mode launch has loaded `egress.toml` allowing `x.example`, and the user runs `allow remove x.example` before the launch reaches the sidecar step (for example during the image build)
- **THEN** the launch does not re-admit `x.example`, and the running sidecar's allowlist matches `egress.toml` after the launch

#### Scenario: Policy becomes invalid during a launch
- **WHEN** `egress.toml` becomes invalid after a launch loaded it but before the sidecar step
- **THEN** the launch fails before starting the sandbox, naming the file and the error, and the running sidecar keeps its previous configuration

#### Scenario: Launch requirements re-checked against the applied policy
- **WHEN** `--agent copilot` is launched, and the Copilot profile is disabled in `egress.toml` before the sidecar step
- **THEN** the launch fails before starting the sandbox with the existing Copilot-profile error, even if the running sidecar still has the profile enabled pending a restart
