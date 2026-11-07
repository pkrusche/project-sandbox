# Spec Delta

## Purpose

The `project-sandbox-egress` executable manages the global egress sidecar of each runtime: starting, stopping and inspecting it, editing the global allowlist, and reading its log.

## ADDED Requirements

### Requirement: A separate executable manages the global sidecar
The package SHALL install a `project-sandbox-egress` executable with the subcommands `up`, `down`, `restart`, `status`, `reload`, `allow` and `log`. Each subcommand SHALL accept `--runtime` and default to the same runtime that `project-sandbox` selects. An unknown subcommand or option SHALL exit non-zero with usage text.

#### Scenario: Help lists the subcommands
- **WHEN** the user runs `project-sandbox-egress --help`
- **THEN** the output lists `up`, `down`, `restart`, `status`, `reload`, `allow` and `log`

#### Scenario: Runtime selection
- **WHEN** the user runs `project-sandbox-egress status --runtime docker`
- **THEN** the command reports the Docker sidecar only

### Requirement: Up starts every sidecar component
`up` SHALL start the global sidecar from `egress.toml` if it is not running and SHALL leave a running, ready sidecar untouched. Starting SHALL bring up Squid on its published endpoint and the host relay when the runtime needs one for the configured upstream. It SHALL return only once the sidecar answers its readiness probe, and it SHALL report the log tail when the probe times out.

#### Scenario: Cold start
- **WHEN** no sidecar exists and the user runs `project-sandbox-egress up`
- **THEN** the sidecar is created, its endpoint answers on the configured host port, and the command exits zero once the sidecar is ready

#### Scenario: Already running
- **WHEN** the sidecar is running and ready and the user runs `up`
- **THEN** no container is created or removed and the command reports that the sidecar is already up

#### Scenario: Readiness timeout
- **WHEN** the sidecar does not answer its readiness probe within the timeout
- **THEN** `up` exits non-zero and prints the last lines of the sidecar log

### Requirement: Down stops the sidecar without tracking users
`down` SHALL stop and remove the sidecar container and the host relay, and SHALL delete the sidecar's CA. It SHALL NOT check whether sandboxes are using the sidecar. Sandboxes still running lose egress.

#### Scenario: Down with running sandboxes
- **WHEN** two sidecar-mode sandboxes are running and the user runs `project-sandbox-egress down`
- **THEN** the sidecar and relay are removed, the CA files are deleted, and the sandboxes' outbound requests fail

#### Scenario: Down when nothing runs
- **WHEN** no sidecar exists and the user runs `down`
- **THEN** the command exits zero

### Requirement: Restart reuses a valid CA
`restart` SHALL stop and start the sidecar with the current `egress.toml`. It SHALL keep the existing CA when that CA is still valid for at least 2 days and its name constraints match the configuration. Otherwise it SHALL generate a new CA. `restart --new-ca` SHALL always generate a new CA.

#### Scenario: Restart keeps the CA
- **WHEN** the CA has 5 days left and the user runs `restart`
- **THEN** the restarted sidecar uses the same CA certificate

#### Scenario: Restart rotates an expiring CA
- **WHEN** the CA has 1 day left and the user runs `restart`
- **THEN** the restarted sidecar uses a newly generated CA

### Requirement: Status reports state without changing it
`status` SHALL report the sidecar's state without creating, starting or removing anything: running and ready, the published endpoint, image currency, configuration drift against `egress.toml` (each field that needs a reload or a restart), CA constraints and expiry, relay state and the policy path. `status --json` SHALL emit the same as JSON.

#### Scenario: Stale configuration
- **WHEN** `egress.toml` gained an allowlist entry after the sidecar started
- **THEN** `status` reports that the allowlist differs and names `project-sandbox-egress reload`

#### Scenario: Port drift
- **WHEN** `egress.toml` sets a different port than the running sidecar publishes
- **THEN** `status` reports the port as needing a restart

#### Scenario: Sidecar absent
- **WHEN** no sidecar exists
- **THEN** `status` reports it as not running and exits zero

### Requirement: Policy edits apply to the running sidecar without a restart
`allow add|remove HOST…` SHALL validate each host with the same rules as the policy file, update `egress.toml`, and apply the change to the running sidecar without restarting it. `reload` SHALL apply the current `egress.toml` allowlist to the running sidecar in the same way. `allow list` SHALL print the effective allowlist and say which entries come from the agent provider set. The editing commands SHALL keep the file's comments and unrelated keys intact.

#### Scenario: Add a host live
- **WHEN** a sandbox is running and the user runs `project-sandbox-egress allow add pypi.org`
- **THEN** the sandbox's next request to `pypi.org` is allowed without the sidecar being restarted

#### Scenario: GitHub repository host
- **WHEN** the user runs `allow add github.com` or `allow add '*.githubusercontent.com'`
- **THEN** the command exits non-zero naming the GitHub repository hosts it would admit, and `egress.toml` is unchanged

#### Scenario: Invalid host
- **WHEN** the user runs `allow add 10.0.0.1`
- **THEN** the command exits non-zero, explaining that IP literals are not accepted

### Requirement: Log gives access to the sidecar's decisions
`log` SHALL print the sidecar's access log. It SHALL support:
- `--follow`;
- `--denied`, which shows only denials with their cause and attribution;
- `--since TIME`, which accepts an ISO 8601 timestamp or a duration such as `10m`.

#### Scenario: Denials since a time
- **WHEN** the user runs `project-sandbox-egress log --denied --since 2026-11-06T10:42:13`
- **THEN** only denials logged at or after that time are printed, each with its cause
