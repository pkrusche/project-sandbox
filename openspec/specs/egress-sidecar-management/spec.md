# egress-sidecar-management Specification

## Purpose
The `project-sandbox-egress` executable manages the global egress sidecar of each runtime: starting, stopping and inspecting it, editing the global allowlist and local-service forwarders, and reading its log.

## Requirements

### Requirement: A separate executable manages the global sidecar
The package SHALL install a `project-sandbox-egress` executable with the subcommands `up`, `down`, `restart`, `status`, `reload`, `allow`, `forward` and `log`. Each subcommand SHALL accept `--runtime` and default to the same runtime that `project-sandbox` selects. An unknown subcommand or option SHALL exit non-zero with usage text.

#### Scenario: Help lists the subcommands
- **WHEN** the user runs `project-sandbox-egress --help`
- **THEN** the output lists `up`, `down`, `restart`, `status`, `reload`, `allow`, `forward` and `log`

#### Scenario: Runtime selection
- **WHEN** the user runs `project-sandbox-egress status --runtime docker`
- **THEN** the command reports the Docker sidecar only

### Requirement: Up starts every sidecar component
`up` SHALL start the global sidecar from `egress.toml` if it is not running and SHALL leave a running, ready sidecar untouched. Starting SHALL bring up Squid, every configured local-service forwarder, and the host relay when the runtime needs one for the configured upstream. It SHALL return only once the sidecar answers its readiness probe, and it SHALL report the log tail when the probe times out.

#### Scenario: Cold start
- **WHEN** no sidecar exists and the user runs `project-sandbox-egress up`
- **THEN** the internal network and the sidecar are created, every configured forwarder listens, and the command exits zero once the sidecar is ready

#### Scenario: Already running
- **WHEN** the sidecar is running and ready and the user runs `up`
- **THEN** no container or network is created or removed and the command reports that the sidecar is already up

#### Scenario: Readiness timeout
- **WHEN** the sidecar does not answer its readiness probe within the timeout
- **THEN** `up` exits non-zero and prints the last lines of the sidecar log

### Requirement: Down stops the sidecar without tracking users
`down` SHALL stop and remove the sidecar container and the host relay, and SHALL delete the sidecar's CA. It SHALL NOT check whether sandboxes are using the sidecar. It SHALL remove the internal network when nothing else is attached, and otherwise SHALL leave the network in place for the next `up`. Sandboxes still running lose egress.

#### Scenario: Down with running sandboxes
- **WHEN** two sidecar-mode sandboxes are running and the user runs `project-sandbox-egress down`
- **THEN** the sidecar and relay are removed, the CA files are deleted, the network remains because sandboxes are attached, and the sandboxes' outbound requests fail

#### Scenario: Down when idle
- **WHEN** no sandbox is attached and the user runs `down`
- **THEN** the sidecar, the relay, the CA files and the internal network are all removed

### Requirement: Restart reuses a valid CA
`restart` SHALL stop and start the sidecar with the current `egress.toml`. It SHALL keep the existing CA when that CA is still valid for at least 2 days and its name constraints match the configuration. Otherwise it SHALL generate a new CA. `restart --new-ca` SHALL always generate a new CA.

#### Scenario: Restart keeps the CA
- **WHEN** the CA has 5 days left and the user runs `restart`
- **THEN** the restarted sidecar uses the same CA certificate

#### Scenario: Restart rotates an expiring CA
- **WHEN** the CA has 1 day left and the user runs `restart`
- **THEN** the restarted sidecar uses a newly generated CA

### Requirement: Status reports state without changing it
`status` SHALL report the sidecar's state without creating, starting or removing anything: running and ready, internal IP, image currency, configuration drift against `egress.toml` (each field that needs a reload or a restart), CA constraints and expiry, relay state, forwarded ports, attached containers and the policy path. `status --json` SHALL emit the same as JSON.

#### Scenario: Stale configuration
- **WHEN** `egress.toml` gained a forwarded port after the sidecar started
- **THEN** `status` reports that the forwarder configuration differs and names the command that applies it

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

### Requirement: Forwarder edits apply live
`forward add|remove PORT…` SHALL update `egress.toml` and start or stop the corresponding forwarder in the running sidecar without restarting it. Ports below 1024 and the proxy port SHALL be rejected. `forward list` SHALL print the configured and the running forwarders.

#### Scenario: Add a forwarder
- **WHEN** the sidecar is running and the user runs `project-sandbox-egress forward add 8787`
- **THEN** the sidecar listens on 8787 on the internal network without a restart, and `egress.toml` lists 8787

#### Scenario: Privileged port
- **WHEN** the user runs `forward add 80`
- **THEN** the command exits non-zero and `egress.toml` is unchanged

### Requirement: Log gives access to the sidecar's decisions
`log` SHALL print the sidecar's access log. It SHALL support:
- `--follow`;
- `--denied`, which shows only denials with their cause;
- `--since TIME`, which accepts an ISO 8601 timestamp or a duration such as `10m`.

Every access-log line SHALL include the client IP address.

#### Scenario: Denials since a time
- **WHEN** the user runs `project-sandbox-egress log --denied --since 2026-11-06T10:42:13`
- **THEN** only denials logged at or after that time are printed, each with its cause and client IP
