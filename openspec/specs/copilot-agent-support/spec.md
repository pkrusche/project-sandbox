# copilot-agent-support

## Purpose

Support the GitHub Copilot CLI (`@github/copilot`, binary `copilot`) as a selectable agent in project-sandbox, running on a Copilot subscription (personal, Business, or Enterprise on GitHub.com or a GHE.com data-residency tenant) with a host-resolved token that reaches only the Copilot process.

## Requirements
### Requirement: Copilot is a selectable agent
The CLI SHALL accept `"copilot"` as a value for `--agent`, alongside `claude`, `codex`, `opencode`, `pi`, and `bash`. The image SHALL install `@github/copilot` at a pinned version via a dedicated `install_copilot` template flag, and the pin SHALL be maintained by `scripts/update-pins.py`.

#### Scenario: Selecting Copilot
- **WHEN** the user runs the CLI with `--agent copilot`
- **THEN** the CLI accepts the value and builds the container using the Copilot agent path

#### Scenario: Image installs the pinned binary
- **WHEN** the image is built with Copilot enabled
- **THEN** the rendered Dockerfile contains `npm install -g @github/copilot@<pinned-version>`

#### Scenario: Incompatible options
- **WHEN** `--agent copilot` is combined with `--agent-proxy` or `--fast`
- **THEN** the CLI exits with an error before any secret lookup, file write, or container start

### Requirement: Copilot host is GitHub.com or a GHE.com tenant
The CLI SHALL provide `--copilot-host HOST`, defaulting to `github.com`. It SHALL accept exactly `github.com` or a single-label `<tenant>.ghe.com` host, and SHALL reject any other value, including self-hosted GitHub Enterprise Server hosts, with an error stating that only GitHub.com and GHE.com are supported. For a tenant host, the container SHALL set `COPILOT_GH_HOST` to that host. For `github.com` it SHALL NOT set `COPILOT_GH_HOST`.

#### Scenario: Default host for personal, Business, and GHEC accounts
- **WHEN** the user runs `--agent copilot` without `--copilot-host`
- **THEN** the token is resolved for `github.com` and `COPILOT_GH_HOST` is not set in the container

#### Scenario: Data-residency tenant
- **WHEN** the user runs `--agent copilot --copilot-host acme.ghe.com`
- **THEN** the token is resolved for `acme.ghe.com` and the container sets `COPILOT_GH_HOST=acme.ghe.com`

#### Scenario: Unsupported host
- **WHEN** the user passes `--copilot-host ghe.corp.example`
- **THEN** the CLI exits with an error naming the supported host forms

### Requirement: Copilot token resolution prefers a pass-stored PAT
At launch, for `--agent copilot`, the system SHALL first try `pass show <entry>`, and SHALL fall back to `gh auth token --hostname <host>` only when that lookup yields no token. The entry SHALL default to `copilot-github-token` for `github.com` and `copilot-github-token/<host>` for a tenant, and `--copilot-token-pass ENTRY` SHALL override it. If neither source yields a token, the CLI SHALL exit with an error naming both remedies.

#### Scenario: PAT found in pass
- **WHEN** `pass show copilot-github-token` returns a token
- **THEN** that token is used, `gh` is not invoked, and no token-scope warning is printed

#### Scenario: Fallback to gh
- **WHEN** the default pass entry is unavailable and `gh auth token --hostname github.com` returns a token
- **THEN** that token is used and the CLI prints a `[W]` warning that the agent holds the GitHub CLI token. The warning points to `docs/security.md` and names the pass entry where a fine-grained PAT can be stored.

#### Scenario: No token available
- **WHEN** neither pass nor gh yields a token
- **THEN** the CLI exits with an error that mentions `pass insert <entry>` and `gh auth login --hostname <host>`, and no container starts

### Requirement: An explicit pass entry never falls back
When `--copilot-token-pass` is given and the `pass` lookup fails, the CLI SHALL exit with an error and SHALL NOT invoke `gh`.

#### Scenario: Explicit entry missing
- **WHEN** `--copilot-token-pass work/copilot` is given and `pass show work/copilot` fails
- **THEN** the CLI exits with an error and does not invoke `gh`

### Requirement: Copilot tokens are validated, redacted, and never taken from argv
The system SHALL reject tokens with the classic `ghp_` prefix, SHALL NOT accept a token through a command-line value, SHALL redact resolved tokens from all output, and SHALL perform no token lookup under `--dry-run`.

#### Scenario: Classic PAT rejected
- **WHEN** the resolved token starts with `ghp_`
- **THEN** the CLI exits with an error stating that classic PATs are not supported by Copilot

#### Scenario: Dry run performs no lookup
- **WHEN** `--agent copilot --dry-run` is used
- **THEN** neither `pass` nor `gh` is invoked, no token file is written, and the preview shows the planned token source and mount

### Requirement: Copilot token reaches only the Copilot process
The system SHALL stage the resolved token as a mode-0600 file outside the repository on every launch, and SHALL mount it read-only at `/project-sandbox-secrets/copilot` only when Copilot is the selected agent. The Copilot entrypoint arms SHALL export `COPILOT_GITHUB_TOKEN` from that file only for the Copilot process. The token SHALL NOT be copied into `$COPILOT_HOME`, the image, or generated project files.

#### Scenario: Copilot run mounts the token
- **WHEN** a container is launched with `--agent copilot`
- **THEN** the run argv contains a read-only mount at `/project-sandbox-secrets/copilot` and the generated files in the project contain no token

#### Scenario: Bash session excludes the token
- **WHEN** a container is launched with `--agent bash`, whether or not credentials are forwarded
- **THEN** no `/project-sandbox-secrets/copilot` mount exists and the process environment has no `COPILOT_GITHUB_TOKEN`

#### Scenario: Other agents exclude the token
- **WHEN** a container is launched with `--agent claude`, `codex`, `opencode`, or `pi`
- **THEN** no Copilot token is resolved or mounted

### Requirement: Copilot implies GitHub network access, chosen explicitly
`--agent copilot` SHALL enable the same allowlist as `--allow-github`. When `--copilot-host` is a GHE.com tenant, the firewall SHALL also DNS-pin that tenant's Copilot and API hosts. The system SHALL NOT enable GitHub access based on the content of a bash prompt, and the `_uses_github_copilot_cli` heuristic SHALL be removed.

#### Scenario: Copilot on github.com
- **WHEN** the user runs `--agent copilot` with the firewall enabled
- **THEN** the rendered firewall includes the GitHub and Copilot hosts that `--allow-github` enables

#### Scenario: Copilot on a tenant
- **WHEN** the user runs `--agent copilot --copilot-host acme.ghe.com`
- **THEN** the rendered firewall additionally pins the `acme.ghe.com` tenant hosts

#### Scenario: Bash prompt invoking copilot
- **WHEN** the user runs `--agent bash --prompt-text 'copilot -p hi'` without `--allow-github`
- **THEN** the rendered firewall does not include the GitHub allowlist

### Requirement: Copilot runs non-interactively inside the sandbox posture
The entrypoint SHALL dispatch interactive Copilot as `copilot --allow-all --no-auto-update --disable-builtin-mcps`. It SHALL dispatch headless Copilot as `copilot -p "$PROMPT" --allow-all --no-ask-user --output-format json --no-auto-update --disable-builtin-mcps`, passing any injected `--model` and `--effort` as two separate flags.

#### Scenario: Headless run
- **WHEN** a headless run is launched with Copilot, `--model gpt-5-mini`, and `--effort high`
- **THEN** the dispatched argv contains `-p`, `--allow-all`, `--no-ask-user`, `--output-format json`, `--no-auto-update`, `--disable-builtin-mcps`, `--model gpt-5-mini`, and `--effort high`

#### Scenario: Interactive run
- **WHEN** an interactive run is launched with Copilot
- **THEN** the entrypoint executes `copilot --allow-all --no-auto-update --disable-builtin-mcps` without `-p`

### Requirement: Copilot home is seeded with a trusted workspace
The container SHALL set `COPILOT_HOME=/home/agent/.copilot` and `COPILOT_AUTO_UPDATE=false`, and SHALL seed a writable `$COPILOT_HOME/config.json` from a read-only baked config that lists `/workspace` as a trusted folder.

#### Scenario: Workspace is trusted
- **WHEN** the container provisions Copilot
- **THEN** `$COPILOT_HOME/config.json` exists, is writable by the agent user, and lists `/workspace` in its trusted folders

### Requirement: Copilot headless output is transcribed and failures exit nonzero
The system SHALL render Copilot's JSONL event stream into the markdown transcript and live view, rendering unknown record kinds as compact raw JSON without raising. When a headless Copilot run exits 0 but the final record of its stream is unambiguously an error, the CLI SHALL report exit code 1, or 75 when that record identifies a rate limit. Otherwise it SHALL keep the original exit code.

#### Scenario: Successful run
- **WHEN** a headless Copilot run emits assistant and tool records and exits 0
- **THEN** a markdown transcript titled for Copilot is written and the exit code stays 0

#### Scenario: Unknown records
- **WHEN** the stream contains a record kind the renderer does not recognize
- **THEN** the transcript includes it as raw JSON and rendering completes

#### Scenario: Error recorded with exit 0
- **WHEN** the run exits 0 and its final record is an error
- **THEN** the CLI reports exit code 1

### Requirement: Copilot is a no-op for OAuth refresh and token expiry
The system SHALL NOT attempt OAuth refresh delegation or token-expiry parsing for Copilot, and both modules SHALL treat `"copilot"` as an unrecognized agent without raising.

#### Scenario: Pre-flight checks with Copilot
- **WHEN** the CLI runs its refresh and expiry checks with `--agent copilot`
- **THEN** refresh silently no-ops, the expiry lookup returns `None`, and the `copilot` CLI is not invoked on the host

### Requirement: Opt-in Copilot end-to-end verification
The repository SHALL provide `scripts/e2e-copilot.py`, registered as an opt-in, billable `copilot` suite in `scripts/run-e2e-tests.sh` that runs only with `--with-copilot`, `--only copilot`, or `--all-tests`. With a personal account it SHALL check a successful headless run, a failing run's exit code, trusted-folder seeding, and network and token isolation. With `--copilot-host <tenant>.ghe.com`, it SHALL also check the tenant path.

#### Scenario: Suite not selected
- **WHEN** `scripts/run-e2e-tests.sh` runs without `--with-copilot`, `--only copilot`, or `--all-tests`
- **THEN** the `copilot` suite is skipped and no token lookup occurs

#### Scenario: Personal-account run
- **WHEN** the suite runs with a personal account and no `--copilot-host`
- **THEN** the `headless-ok`, `headless-fail`, `trusted-folder`, and `isolation` checks run and the `tenant` check is reported as skipped

#### Scenario: Tenant run
- **WHEN** the suite runs with `--copilot-host acme.ghe.com`
- **THEN** the `tenant` check resolves the tenant token and completes a headless run through the pinned tenant hosts

### Requirement: Copilot e2e captures are redacted and unit tests stay offline
The e2e script SHALL support `--capture-dir` for JSONL and exit-code captures, and SHALL redact the token from captures and printed output. Unit tests for the script SHALL NOT require Copilot, a token, or a container runtime.

#### Scenario: Captures written
- **WHEN** the suite runs with `--capture-dir DIR`
- **THEN** each check's JSONL stream and exit code are written under `DIR` with the token replaced by a redaction marker

#### Scenario: Unit tests offline
- **WHEN** `uv run pytest -q` runs without Copilot, a token, or a container runtime
- **THEN** `test_e2e_copilot.py` passes
