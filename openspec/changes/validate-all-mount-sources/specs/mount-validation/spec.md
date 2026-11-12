# Spec Delta

## Purpose

Defines the source and target rules every user-supplied `--mount` value must pass, in every egress mode, before a container or devcontainer configuration uses it.

## ADDED Requirements

### Requirement: Bind sources never expose project-sandbox's protected locations
In every egress mode, a bind source SHALL be refused when it is, contains or lies inside the egress policy directory, the egress state directory or the project-sandbox state root.

#### Scenario: Sidecar state in firewall mode
- **WHEN** a firewall-mode launch passes `--mount source=<egress state>/docker/sidecar,target=/x`
- **THEN** the launch fails before starting a container, naming the egress state directory

#### Scenario: Home directory
- **WHEN** `--mount source=$HOME,target=/home-copy` is given
- **THEN** the launch fails, naming the protected location the source contains

### Requirement: Credential directories are never mounted
A bind source SHALL be refused when it is, contains or lies inside a well-known credential directory: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/.password-store` or an agent's host config directory. The error SHALL name the directory and explain that the mount would bypass per-agent credential forwarding. No option SHALL override the refusal.

#### Scenario: Mounting SSH keys
- **WHEN** `--mount source=$HOME/.ssh,target=/home/agent/.ssh,readonly` is given
- **THEN** the launch fails before starting a container, naming `~/.ssh`

#### Scenario: Subdirectory of a credential directory
- **WHEN** `--mount source=$HOME/.aws/sso,target=/x` is given
- **THEN** the launch fails, naming `~/.aws`

#### Scenario: Agent config directory
- **WHEN** `--mount source=$HOME/.claude,target=/x` is given
- **THEN** the launch fails, naming the directory and pointing to credential forwarding

### Requirement: Mount targets avoid image-managed locations
A mount target SHALL be absolute and SHALL contain no `..` or control characters. It SHALL NOT be `/workspace` itself, and SHALL NOT lie under `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/root`, `/proc`, `/sys`, `/dev`, `/run` or any `/project-sandbox-*` directory. Mounts that project-sandbox adds for protection SHALL be applied after user mounts.

#### Scenario: Writable directory over image scripts
- **WHEN** `--mount source=<project>/tools,target=/usr/local/bin` is given
- **THEN** the launch fails, naming `/usr` as a protected target location

#### Scenario: Mount over a masked path
- **WHEN** `--mount source=<dir>,target=/workspace/.devcontainer` is given
- **THEN** the session's read-only mask over `/workspace/.devcontainer` is still in effect inside the container

### Requirement: Workspace-relative sources stay inside the workspace
A bind source that lies inside the project or workspace SHALL be refused if it resolves outside it. The resolved path SHALL be what the runtime receives.

#### Scenario: Symlink planted by an earlier session
- **WHEN** `<project>/data` is a symlink to `$HOME/.ssh` and `--mount source=<project>/data,target=/data` is given
- **THEN** the launch fails, stating that the source resolves outside the project

### Requirement: Devcontainer mounts pass the same rules
User `--mount` values written into a generated devcontainer configuration SHALL pass the same source and target rules.

#### Scenario: Credential directory in a devcontainer
- **WHEN** the devcontainer is rendered with `--mount source=$HOME/.ssh,target=/x`
- **THEN** rendering fails naming `~/.ssh`, and no devcontainer file is written
