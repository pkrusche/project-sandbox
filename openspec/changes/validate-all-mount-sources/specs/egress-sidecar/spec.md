# Spec Delta

## MODIFIED Requirements

### Requirement: Policy lives on the host outside the sandbox's reach
The system SHALL read sidecar policy only from `$XDG_CONFIG_HOME/project-sandbox/egress.toml`; there SHALL be no per-launch policy flag. The effective allowlist SHALL be the policy's `allow` entries plus the provider hosts of every supported agent. A sidecar-mode launch SHALL be rejected when the policy file or the sidecar state directory resolves inside the project, the worktree, the main repository's `.git`, or any other directory mounted into the sandbox. A launch in any egress mode SHALL be rejected when any mount source, in any mount form, resolves to, inside, or around the policy directory or the sidecar state directory. The system SHALL reject unknown keys and invalid hostnames. Rendered sidecar config SHALL be written to the sidecar state directory, not to the project.

#### Scenario: Policy inside the project
- **WHEN** a sidecar-mode launch mounts a directory that contains `egress.toml` or the sidecar state directory
- **THEN** the CLI exits with an error explaining that the agent could modify the policy or read the CA key

#### Scenario: Provider hosts are always allowed
- **WHEN** `egress.toml` allows only `pypi.org`
- **THEN** the sidecar admits `pypi.org` and the provider hosts of every supported agent

#### Scenario: Effective policy preview
- **WHEN** the user runs with `--egress sidecar --dry-run`
- **THEN** the output shows the policy path, the effective allowlist, the Copilot profile, intercepted hosts, CA constraints, the published endpoint and whether the sidecar would be started or reused, and no file, key, or container is created

#### Scenario: Unknown key
- **WHEN** the policy file contains an unrecognized key
- **THEN** the CLI exits with an error naming the key

#### Scenario: Mount inside the sidecar state directory
- **WHEN** a launch in any egress mode mounts a subdirectory of the sidecar state directory, for example the one holding the CA key
- **THEN** the CLI exits with an error naming the protected location

#### Scenario: Unmodeled mount option
- **WHEN** a mount of the policy directory carries an extra option such as `bind-propagation=rshared`
- **THEN** the mount is rejected and never passed to the runtime
