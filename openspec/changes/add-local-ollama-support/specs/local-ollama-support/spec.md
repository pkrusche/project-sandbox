## ADDED Requirements

### Requirement: Pi-Ollama is opt-in via a dedicated flag
The CLI SHALL accept a `--pi-ollama` flag that has no effect unless `--agent pi` is also selected, and SHALL leave all existing behavior unchanged when the flag is absent.

#### Scenario: Flag passed with Pi selected
- **WHEN** the user runs the CLI with `--agent pi --pi-ollama`
- **THEN** the CLI establishes a verified runtime-specific path to loopback-bound Ollama, enables port-scoped firewall access to that endpoint, and bakes Pi's Ollama provider configuration

#### Scenario: Flag passed without Pi selected
- **WHEN** the user runs the CLI with `--pi-ollama` but `--agent` is not `pi`
- **THEN** the CLI proceeds without enabling any Ollama-specific firewall or config behavior

#### Scenario: Flag absent
- **WHEN** the user runs the CLI without `--pi-ollama`
- **THEN** no Ollama forwarding resource is created, no endpoint allow rule for the Ollama port is added, and no Pi Ollama config is baked, regardless of firewall settings otherwise in effect

### Requirement: Ollama remains bound to host loopback
When `--pi-ollama` is set, the system SHALL reach an Ollama server listening on `127.0.0.1:11434` through a verified runtime-specific forwarding path. The system SHALL prefer a runtime-native loopback-forwarding mechanism and SHALL use a managed `socat` bridge proxy only when the selected local Linux bridge runtime lacks a native mechanism. It SHALL NOT bind any listener to `0.0.0.0` or require Ollama to bind beyond loopback.

#### Scenario: Loopback-bound Ollama is reachable
- **WHEN** Ollama is listening on `127.0.0.1:11434` and the user starts a sandbox with `--agent pi --pi-ollama`
- **THEN** the sandbox reaches Ollama through the selected forwarding path without changing Ollama's bind address

#### Scenario: Runtime-native forwarding is available
- **WHEN** the selected runtime provides a verified native mapping from the container to host loopback
- **THEN** the system uses that mapping without starting `socat`

#### Scenario: Apple localhost DNS is not preconfigured
- **WHEN** Apple `container` is selected and `ollama.project-sandbox.internal` has not been configured with the runtime's localhost DNS facility
- **THEN** startup fails without invoking `sudo` or changing host networking and prints the exact administrator command the user can run manually

#### Scenario: Local Linux bridge fallback is required
- **WHEN** the selected runtime uses a local Linux bridge whose host bridge address is bindable and no native loopback mapping is available
- **THEN** the system starts `socat` on that exact bridge address and forwards to `127.0.0.1:11434`

#### Scenario: No safe forwarding path
- **WHEN** the selected runtime mode provides neither verified native forwarding nor a safe, host-bindable bridge address
- **THEN** startup fails with a clear unsupported-mode error and does not fall back to a wildcard listener

### Requirement: Ollama forwarding resources have a bounded lifecycle
The system SHALL verify forwarding before starting the sandbox, track resources it creates, detect setup failure, and remove only its owned forwarding resources when the sandbox exits or container startup fails. It SHALL NOT create or remove Apple `container` system DNS mappings or invoke `sudo`. When the selected adapter requires `socat`, the system SHALL verify it is available and terminate and reap its managed child process.

#### Scenario: socat is unavailable
- **WHEN** the selected adapter requires `socat` and it is not installed on the host
- **THEN** startup fails before launching the container with an actionable error

#### Scenario: Proxy cannot listen
- **WHEN** the selected runtime-private address and port cannot be bound
- **THEN** startup fails before launching the container and reports the proxy failure

#### Scenario: Native forwarding setup fails
- **WHEN** the selected runtime's native mapping cannot be verified or established
- **THEN** startup fails before launching the container and reports the runtime-specific remediation

#### Scenario: Sandbox run ends
- **WHEN** the sandbox container exits normally, is interrupted, or fails to start
- **THEN** owned native-forwarding resources are removed and any managed proxy process is terminated and reaped

### Requirement: Firewall reaches the host's Ollama port only
When `--pi-ollama` is set and the firewall is enabled, the system SHALL determine the adapter-selected endpoint and allow outbound TCP traffic to that endpoint restricted to port 11434, without granting access to any other port on that endpoint.

#### Scenario: Firewall enabled with Pi-Ollama
- **WHEN** the container is launched with `--pi-ollama` and the firewall is not disabled
- **THEN** the rendered firewall setup resolves or receives the selected endpoint and adds an iptables ACCEPT rule scoped to `tcp --dport 11434` for that IP

#### Scenario: Firewall disabled
- **WHEN** the user passes `--no-firewall` alongside `--pi-ollama`
- **THEN** no firewall rules are applied at all (existing `--no-firewall` behavior is unchanged) and Ollama reachability depends entirely on the absence of any firewall

#### Scenario: Direct CLI run without devcontainer's host-network allowance
- **WHEN** `--pi-ollama` is set on a direct (non-devcontainer) CLI run
- **THEN** the gateway-discovery and port-scoped allow rule are applied even though the broader devcontainer-only `allow_host_network` all-ports gateway rule is not active

### Requirement: A fixed hostname resolves to the selected Ollama endpoint
The system SHALL make `ollama.project-sandbox.internal` resolve to the adapter-selected endpoint inside the container when `--pi-ollama` is set and SHALL pin the verified address for the container lifetime where the runtime permits it.

#### Scenario: Container startup with Pi-Ollama enabled
- **WHEN** the container starts with `--pi-ollama` set and the firewall enabled
- **THEN** `ollama.project-sandbox.internal` resolves to the verified runtime-native or bridge-proxy endpoint

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
