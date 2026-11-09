# Spec Delta

## MODIFIED Requirements

### Requirement: Policy edits apply to the running sidecar without a restart
`allow add|remove HOST…` SHALL validate each host with the same rules as the policy file, update `egress.toml`, and apply the change to the running sidecar without restarting it. `reload` SHALL apply the current `egress.toml` allowlist to the running sidecar in the same way. `allow list` SHALL print the effective allowlist and say which entries come from the agent provider set. The editing commands SHALL keep the file's comments and unrelated keys intact. Concurrent edits SHALL NOT lose each other's changes, and every live apply SHALL use the file's contents at the time it applies them.

#### Scenario: Add a host live
- **WHEN** a sandbox is running and the user runs `project-sandbox-egress allow add pypi.org`
- **THEN** the sandbox's next request to `pypi.org` is allowed without the sidecar being restarted

#### Scenario: GitHub repository host
- **WHEN** the user runs `allow add github.com` or `allow add '*.githubusercontent.com'`
- **THEN** the command exits non-zero naming the GitHub repository hosts it would admit, and `egress.toml` is unchanged

#### Scenario: Invalid host
- **WHEN** the user runs `allow add 10.0.0.1`
- **THEN** the command exits non-zero, explaining that IP literals are not accepted

#### Scenario: Concurrent edits
- **WHEN** `allow add a.example` and `allow add b.example` run at the same time
- **THEN** both commands succeed, and `egress.toml` afterwards contains both hosts

#### Scenario: Concurrent add and remove
- **WHEN** `allow add a.example` and `allow remove b.example` run at the same time, starting from a file that allows `b.example`
- **THEN** `egress.toml` afterwards allows `a.example` and not `b.example`, and the running sidecar matches the file

#### Scenario: Hand edit during a command
- **WHEN** the file is changed by another program between an `allow` command's read and its write
- **THEN** the command re-applies its change to the new contents, or exits non-zero without writing, and the other program's change is kept in either case

#### Scenario: Reload applies the current file
- **WHEN** two `allow` commands finish in either order, each followed by its live apply
- **THEN** the running sidecar's allowlist equals the final contents of `egress.toml`

#### Scenario: Applied policy is reported
- **WHEN** `reload`, or the live apply after `allow`, updates the running sidecar
- **THEN** the output includes a short hash of the applied policy contents, and `status` shows the same hash next to the hash of the current file

#### Scenario: Invalid file at apply time
- **WHEN** `egress.toml` is invalid when `reload` runs
- **THEN** the command exits non-zero naming the file and the error, and the running sidecar keeps its previous allowlist
