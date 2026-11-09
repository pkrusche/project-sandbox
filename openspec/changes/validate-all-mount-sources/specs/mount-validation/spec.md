# Spec Delta

## Purpose

Defines which mount forms a sandbox accepts, how their sources and targets are validated, which host locations can never be mounted, and how every mount is encoded for the container runtimes and the generated devcontainer.

## ADDED Requirements

### Requirement: Only modeled mount forms are accepted
The system SHALL accept `--mount` values only as bind, tmpfs or named-volume mounts with the keys and options listed in the scenarios below. Any other type, key or option SHALL be refused, and the error SHALL name it and list the supported forms. No mount value SHALL reach the runtime without being parsed into one of these forms.

#### Scenario: Supported keys
- **WHEN** a bind mount uses only `source`, `target`, `readonly`/`ro`, `bind-propagation=private|rprivate` and `consistency`; a tmpfs mount uses only `target`, `tmpfs-size` and `tmpfs-mode`; or a volume mount uses only `source` (the volume name), `target` and `readonly`
- **THEN** the value is accepted, subject to the source and target rules

#### Scenario: Extra option on a protected bind
- **WHEN** `--mount type=bind,source=<egress state dir>,target=/x,bind-propagation=rshared` is given
- **THEN** the launch fails naming `bind-propagation=rshared` as unsupported

#### Scenario: Supported harmless option
- **WHEN** `--mount type=bind,source=<dir>,target=/data,bind-propagation=rprivate` is given for an allowed directory
- **THEN** the mount is accepted

#### Scenario: tmpfs mount
- **WHEN** `--mount type=tmpfs,target=/scratch,tmpfs-size=64m` is given
- **THEN** the mount is accepted

### Requirement: Bind sources never expose project-sandbox's protected locations
After resolving symlinks, a bind source SHALL be refused when it equals, contains or lies inside any of these locations: the egress policy directory, the egress state directory, project-sandbox's state root, or its runtime root. This SHALL apply in every egress mode.

#### Scenario: Subdirectory of the egress state
- **WHEN** `--mount source=<egress state dir>/docker/sidecar,target=/x` is given in sidecar or firewall mode
- **THEN** the launch fails, naming the protected location

#### Scenario: Home directory
- **WHEN** `--mount source=$HOME,target=/home-host` is given and the state root lies under `$HOME`
- **THEN** the launch fails, naming the protected location it contains

### Requirement: Workspace-relative sources stay inside the workspace
A bind source that lies inside the project or workspace SHALL be refused if it resolves outside it. The resolved path SHALL be what the runtime receives.

#### Scenario: Symlink planted by an earlier session
- **WHEN** `<project>/data` is a symlink to `$HOME/.ssh`, and `--mount source=<project>/data,target=/data` is given
- **THEN** the launch fails, stating that the source resolves outside the project

### Requirement: Credential directories are mounted only with a warning
The system SHALL warn, naming the directory, when a bind source is or contains a well-known credential directory: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/.password-store` or an agent's host config directory. The warning SHALL explain that the mount bypasses per-agent credential filtering. The mount SHALL still be accepted.

#### Scenario: Mounting SSH keys
- **WHEN** `--mount source=$HOME/.ssh,target=/home/agent/.ssh,readonly` is given
- **THEN** the session starts and the CLI prints the credential warning

### Requirement: Named volumes are restricted by egress mode
Named volume mounts SHALL be refused in sidecar mode. In firewall mode they SHALL be accepted, with a warning that their host exposure is not checked.

#### Scenario: Volume in sidecar mode
- **WHEN** `--egress sidecar --mount type=volume,source=cache,target=/cache` is given
- **THEN** the launch fails, stating that volumes cannot be checked in sidecar mode

#### Scenario: Volume in firewall mode
- **WHEN** the same mount is given in firewall mode
- **THEN** the session starts and the CLI prints the warning

### Requirement: Mount targets avoid image-managed locations
A mount target SHALL be absolute and SHALL contain no `..` or control characters. It SHALL NOT be `/workspace` itself, and SHALL NOT lie under `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/root`, `/proc`, `/sys`, `/dev`, `/run` or any `/project-sandbox-*` directory. Mounts that project-sandbox adds for protection SHALL be applied after user mounts.

#### Scenario: Writable directory over image scripts
- **WHEN** `--mount source=<project>/tools,target=/usr/local/bin` is given
- **THEN** the launch fails, naming `/usr` as a protected target location

#### Scenario: Mount over a masked path
- **WHEN** `--mount source=<dir>,target=/workspace/.devcontainer` is given
- **THEN** the session's read-only mask over `/workspace/.devcontainer` is still in effect inside the container

### Requirement: Every mount is encoded by one validating encoder
All mounts, whether from `--mount` or added by project-sandbox, SHALL be rendered for the runtime's `--mount` argument and for `devcontainer.json` by one encoder. The encoder SHALL refuse any field containing `,`, `"`, a control character, or `=` within a path, rather than escape it.

#### Scenario: Comma in a project path
- **WHEN** the project's absolute path contains a comma
- **THEN** the launch fails before starting a container, explaining that the path cannot be mounted safely

#### Scenario: Round trip
- **WHEN** any accepted mount is encoded and parsed again
- **THEN** the parsed value equals the original
