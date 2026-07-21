## ADDED Requirements

### Requirement: Named-agent runs forward only the selected agent's credentials
When credential forwarding is enabled, a direct run for `claude`, `codex`, `opencode`, or `pi` SHALL mount and provision credentials only for the selected agent. Credentials for every other agent SHALL remain unreachable even when they exist in the host credential stores or private staging area.

#### Scenario: Claude run has only Claude credentials
- **WHEN** the host has credentials for all supported agents and the user runs with `--agent claude`
- **THEN** only `/project-sandbox-secrets/claude` is mounted and only Claude credential files are provisioned in the container home

#### Scenario: Codex run has only Codex credentials
- **WHEN** the host has credentials for all supported agents and the user runs with `--agent codex`
- **THEN** only `/project-sandbox-secrets/codex` is mounted and only Codex credential files are provisioned in the container home

#### Scenario: OpenCode run has only OpenCode credentials
- **WHEN** the host has credentials for all supported agents and the user runs with `--agent opencode`
- **THEN** only `/project-sandbox-secrets/opencode` is mounted and only OpenCode credential files are provisioned in the container home

#### Scenario: Pi run has only Pi credentials
- **WHEN** the host has credentials for all supported agents and the user runs with `--agent pi`
- **THEN** only `/project-sandbox-secrets/pi` is mounted and only Pi credential files are provisioned in the container home

#### Scenario: Headless named-agent run uses the base-agent policy
- **WHEN** a named agent is launched in headless mode
- **THEN** credential selection is identical to the corresponding interactive named-agent run

### Requirement: Bash and devcontainers explicitly forward multiple agents
When credential forwarding is enabled, `--agent bash` sessions and generated devcontainers SHALL mount and provision credentials for every supported agent detected on the host. User-facing security and runtime documentation SHALL identify these modes as intentionally multi-agent.

#### Scenario: Bash receives detected credentials
- **WHEN** the host has multiple supported agent credentials and the user runs with `--agent bash`
- **THEN** the bash environment receives each detected agent's staged credentials

#### Scenario: Devcontainer receives detected credentials
- **WHEN** a devcontainer configuration is generated with credential forwarding enabled
- **THEN** its mounts include each detected agent's staged credentials

#### Scenario: Multi-agent exposure is documented
- **WHEN** a user consults credential-forwarding documentation
- **THEN** the documentation states that bash sessions and devcontainers expose all detected forwarded agent credentials

### Requirement: Credential forwarding can be disabled globally
When `--no-forward-credentials` is used, the system SHALL NOT read, stage, mount, or provision credentials for any agent in named-agent, bash, or generated-devcontainer modes, and SHALL purge credentials previously staged for the project.

#### Scenario: Forwarding disabled for a named agent
- **WHEN** the user selects a named agent with `--no-forward-credentials`
- **THEN** no agent secret mount or provisioned credential file is present

#### Scenario: Forwarding disabled for multi-agent modes
- **WHEN** credential forwarding is disabled for a bash run or generated devcontainer
- **THEN** no agent secret mount is configured and previously staged project credentials are removed

### Requirement: Staged credentials are reachable only through authorized secret mounts
Host credentials SHALL be staged in private directories outside the project build context and SHALL NOT be included in generated configuration mounts, the workspace staging directory, or a built image. Direct runtime mount construction SHALL independently enforce the execution mode's credential allowlist even if supplied an over-broad set of staged credential directories.

#### Scenario: Over-broad staged inputs reach a named-agent mount builder
- **WHEN** runtime mount construction for a named agent receives staged credential paths for multiple agents
- **THEN** it emits a secret mount only for the selected agent

#### Scenario: Workspace staging directory is hidden
- **WHEN** the project workspace contains `.project-sandbox` and a sandbox is launched
- **THEN** `/workspace/.project-sandbox` is masked by an empty read-only mount and exposes no staged credential files

#### Scenario: Generated configuration remains non-secret
- **WHEN** agent configuration directories are rendered and mounted
- **THEN** they contain no copied host credential files and cannot provide an alternate path to credentials for an unselected agent

#### Scenario: Built image contains no staged credentials
- **WHEN** a sandbox image is built while host credentials are staged
- **THEN** no staged host credential is copied into an image layer or default secret directory

### Requirement: Explicit custom mounts remain user-authorized
Credential isolation SHALL govern project-sandbox-managed mounts and SHALL NOT claim to prevent a user from explicitly bind-mounting a host credential path with `--mount`.

#### Scenario: User supplies a credential-bearing custom mount
- **WHEN** a user explicitly supplies a `--mount` whose source contains credentials
- **THEN** the mount is treated as user-authorized exposure and is not represented as a project-sandbox-managed credential mount

