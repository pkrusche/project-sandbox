## ADDED Requirements

### Requirement: Pi is a selectable agent
The CLI SHALL accept `"pi"` as a valid value for `--agent`, alongside `claude`, `codex`, and `opencode`.

#### Scenario: Selecting Pi via --agent
- **WHEN** the user runs the CLI with `--agent pi`
- **THEN** the CLI accepts the value without an argparse error and proceeds to build the container using the Pi agent path

#### Scenario: Pi image build installs the pinned binary
- **WHEN** the container image is built with Pi enabled
- **THEN** the Dockerfile installs `@earendil-works/pi-coding-agent` pinned to version `0.80.6` via a dedicated `install_pi` template flag

### Requirement: Pi credentials are mounted as a flat file
The system SHALL sync and mount Pi's credential file (`~/.pi/agent/auth.json`, mode 0600) into the container at `/project-sandbox-secrets/pi`, without mounting any `/project-sandbox-config/pi` path.

#### Scenario: Credentials present on host
- **WHEN** `~/.pi/agent/auth.json` exists on the host and Pi is the selected agent
- **THEN** the file is synced via the generic single-file credential sync path and bind-mounted read-only at `/project-sandbox-secrets/pi` (then copied to `$HOME/.pi/agent/auth.json`) inside the container

#### Scenario: Credentials absent on host
- **WHEN** `~/.pi/agent/auth.json` does not exist on the host
- **THEN** the container is built and run without a `/project-sandbox-secrets/pi` mount, and no error is raised

#### Scenario: No baked config file
- **WHEN** the container is built with Pi enabled
- **THEN** no `/project-sandbox-config/pi` mount or rendered config file is created, matching Pi having no host-renderable configuration

### Requirement: Pi headless dispatch always passes --approve
The entrypoint SHALL dispatch headless Pi runs via `pi -p "<prompt>" --approve`, and SHALL always include `--approve` since Pi has no interactive trust prompt available in headless mode.

#### Scenario: Headless run
- **WHEN** the container is launched in unsupervised/headless mode with Pi selected
- **THEN** the entrypoint's `pi-headless` case arm executes `pi -p "$PROMPT" --approve` (plus any injected model/effort flags)

#### Scenario: Interactive run
- **WHEN** the container is launched in interactive mode with Pi selected
- **THEN** the entrypoint's `pi` case arm executes `exec pi` without forcing `--approve`

### Requirement: Pi model and effort use a single combined flag
When a model and/or effort level is injected for a headless Pi run, the CLI SHALL emit them as one combined flag (`--model <model>:<effort>`), not as two separate `--model`/`--effort` flags.

#### Scenario: Model and effort both specified
- **WHEN** the user runs headless Pi with a model and an effort level set
- **THEN** the injected argv contains a single `--model sonnet:high`-shaped flag and no separate `--effort` flag

#### Scenario: Only model specified
- **WHEN** the user runs headless Pi with only a model set and no effort level
- **THEN** the injected argv contains `--model <model>` with no effort suffix

### Requirement: Pi provider allowlist warning
When Pi is selected and the firewall is enabled, the system SHALL warn that only default-allowlisted providers are reachable and that BYOK providers outside that set require `--allow-github`/`--extra-domain`, using the same generalized warning mechanism as OpenCode.

#### Scenario: Pi selected with firewall enabled
- **WHEN** the user selects `--agent pi` and the firewall is not disabled
- **THEN** the CLI prints a provider-allowlist warning equivalent to the existing OpenCode warning, worded for Pi

#### Scenario: Pi selected with firewall disabled
- **WHEN** the user selects `--agent pi` and passes the flag that disables the firewall
- **THEN** no provider-allowlist warning is printed

### Requirement: Pi telemetry and version-check suppression
The container SHALL set `PI_SKIP_VERSION_CHECK=1` in Pi's runtime environment to suppress update checks.

#### Scenario: Container environment for Pi runs
- **WHEN** a container is launched with Pi as the selected agent
- **THEN** the process environment includes `PI_SKIP_VERSION_CHECK=1`

### Requirement: Pi is excluded from OAuth refresh and token-expiry reporting
The system SHALL NOT attempt OAuth refresh delegation or token-expiry parsing for Pi, and SHALL treat Pi as an unrecognized agent in `oauth_refresh.py` and `token_expiry.py` without raising an error.

#### Scenario: Refresh check with Pi selected
- **WHEN** the CLI runs its pre-flight OAuth refresh check with `--agent pi`
- **THEN** the refresh dispatch silently no-ops for Pi and does not raise

#### Scenario: Token expiry check with Pi selected
- **WHEN** the CLI evaluates credential expiry with `--agent pi`
- **THEN** the expiry lookup returns `None` for Pi and does not raise
