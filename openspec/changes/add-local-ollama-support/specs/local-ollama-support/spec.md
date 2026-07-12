## ADDED Requirements

### Requirement: Pi-Ollama is opt-in via a dedicated flag
The CLI SHALL accept a `--pi-ollama` flag that has no effect unless `--agent pi` is also selected, and SHALL leave all existing behavior unchanged when the flag is absent.

#### Scenario: Flag passed with Pi selected
- **WHEN** the user runs the CLI with `--agent pi --pi-ollama`
- **THEN** the CLI enables gateway firewall access to the host's Ollama port and bakes Pi's Ollama provider configuration

#### Scenario: Flag passed without Pi selected
- **WHEN** the user runs the CLI with `--pi-ollama` but `--agent` is not `pi`
- **THEN** the CLI proceeds without enabling any Ollama-specific firewall or config behavior

#### Scenario: Flag absent
- **WHEN** the user runs the CLI without `--pi-ollama`
- **THEN** no gateway allow rule for the Ollama port is added and no Pi Ollama config is baked, regardless of firewall settings otherwise in effect

### Requirement: Firewall reaches the host's Ollama port only
When `--pi-ollama` is set and the firewall is enabled, the system SHALL discover the container's default-gateway IP and allow outbound TCP traffic to that IP restricted to the Ollama port (11434), without granting access to any other port on the gateway IP.

#### Scenario: Firewall enabled with Pi-Ollama
- **WHEN** the container is launched with `--pi-ollama` and the firewall is not disabled
- **THEN** the rendered firewall script discovers the gateway IP and adds an iptables ACCEPT rule scoped to `tcp --dport 11434` for that IP

#### Scenario: Firewall disabled
- **WHEN** the user passes `--no-firewall` alongside `--pi-ollama`
- **THEN** no firewall rules are applied at all (existing `--no-firewall` behavior is unchanged) and Ollama reachability depends entirely on the absence of any firewall

#### Scenario: Direct CLI run without devcontainer's host-network allowance
- **WHEN** `--pi-ollama` is set on a direct (non-devcontainer) CLI run
- **THEN** the gateway-discovery and port-scoped allow rule are applied even though the broader devcontainer-only `allow_host_network` all-ports gateway rule is not active

### Requirement: A fixed hostname resolves to the host gateway inside the container
The system SHALL pin the hostname `ollama.project-sandbox.internal` to the discovered gateway IP in the container's `/etc/hosts` at startup when `--pi-ollama` is set, using the same pinning mechanism already used for allowlisted domains.

#### Scenario: Container startup with Pi-Ollama enabled
- **WHEN** the container starts with `--pi-ollama` set and the firewall enabled
- **THEN** `/etc/hosts` inside the container contains an entry mapping `ollama.project-sandbox.internal` to the discovered gateway IP

#### Scenario: No dynamic address exposed to the agent process
- **WHEN** the container starts with `--pi-ollama` set
- **THEN** no `OLLAMA_HOST` (or equivalent) environment variable is set, and Pi's provider configuration references the fixed hostname rather than a runtime-discovered value

### Requirement: Pi's Ollama provider and default model are pre-configured
When `--pi-ollama` is set, the system SHALL bake `~/.pi/agent/models.json` with an `ollama` provider entry pointing at `http://ollama.project-sandbox.internal:11434/v1` using the `openai-completions` API shape, and SHALL bake `~/.pi/settings.json` setting `defaultProvider` to `ollama` and `defaultModel` to a configured model ID.

#### Scenario: Default model list
- **WHEN** `--pi-ollama` is set without any `--ollama-model` flags
- **THEN** `models.json`'s `ollama` provider is populated with the built-in default model ID list

#### Scenario: Custom model list
- **WHEN** the user passes one or more `--ollama-model <id>` flags alongside `--pi-ollama`
- **THEN** `models.json`'s `ollama` provider's model list reflects the user-supplied IDs instead of the built-in defaults

#### Scenario: No baked config without the flag
- **WHEN** `--agent pi` is selected without `--pi-ollama`
- **THEN** no `/project-sandbox-config/pi` mount is created and neither `models.json` nor `settings.json` is baked, matching existing Pi behavior (BYOK, no host-renderable configuration)

### Requirement: Baked Pi config is mounted to its distinct target paths
The system SHALL mount the baked `models.json` to `~/.pi/agent/models.json` and the baked `settings.json` to `~/.pi/settings.json` inside the container — two distinct paths, since `settings.json` lives outside the `~/.pi/agent` directory tree used for Pi's synced credentials.

#### Scenario: Both files present after container start
- **WHEN** the container starts with `--pi-ollama` set
- **THEN** both `~/.pi/agent/models.json` and `~/.pi/settings.json` exist inside the container with the baked content, without disturbing the separately-synced `~/.pi/agent/auth.json` credential file
