# Spec Delta

## MODIFIED Requirements

### Requirement: Destinations are filtered by hostname with SNI verification
The sidecar SHALL deny every request unless its destination hostname matches the effective allowlist, exactly or through a `*.` wildcard. A wildcard `*.example.com` SHALL match only names with at least one label before `example.com`, never `example.com` itself. A wildcard whose base is a public suffix, or has a public suffix beneath it (ICANN or private section of the bundled Public Suffix List), SHALL be rejected when the policy is loaded or edited. The sidecar SHALL deny plain HTTP to every host not listed in `[egress] http_allow`, and agent provider hosts SHALL NOT be accepted in `http_allow`. It SHALL deny CONNECT to ports other than 443, IP-literal destinations, and (when no parent proxy is set) cloud-metadata and private, loopback, and link-local resolved addresses. All of these denials SHALL be issued before any upstream connection is made, and the sidecar SHALL record the denial cause in its access log. Port, IP-literal and allowlist denials SHALL close the client connection without responding `200 Connection established`. A resolved-address denial (which needs a DNS lookup) MAY answer `200` before the client's TLS handshake fails, but SHALL NOT open the upstream connection. For allowed CONNECTs, the sidecar SHALL peek at the TLS ClientHello and SHALL terminate the connection when the SNI is missing, is not allowlisted, or differs from the CONNECT host, or when the tunnel does not carry TLS.

#### Scenario: Unlisted host
- **WHEN** a client sends `CONNECT example.org:443` and `example.org` is not allowlisted
- **THEN** the sidecar closes the connection without responding `200 Connection established`, opens no upstream connection, and logs the cause `host-not-allowed`

#### Scenario: Wildcard host
- **WHEN** the allowlist contains `*.npmjs.org` and a client connects to `registry.npmjs.org`
- **THEN** the connection is allowed and spliced

#### Scenario: Wildcard does not match the apex
- **WHEN** the allowlist contains `*.example.com` but not `example.com`, and a client sends `CONNECT example.com:443`
- **THEN** the connection is denied with cause `host-not-allowed`

#### Scenario: Public-suffix wildcard
- **WHEN** `egress.toml` allows `*.co.uk` or `*.github.io`, or the user runs `project-sandbox-egress allow add '*.amazonaws.com'`
- **THEN** the policy is rejected with an error naming the public suffix the wildcard is or would cover

#### Scenario: Plain HTTP denied by default
- **WHEN** a client requests `http://pypi.org/simple/` and `pypi.org` is allowlisted but not in `http_allow`
- **THEN** the request is denied with a labelled page and cause `plain-http-not-allowed`

#### Scenario: Plain HTTP exception
- **WHEN** `[egress] http_allow = ["deb.debian.org"]` and a client requests `http://deb.debian.org/debian/`
- **THEN** the request is allowed

#### Scenario: Provider host in http_allow
- **WHEN** `http_allow` contains `api.anthropic.com`
- **THEN** the policy is rejected

#### Scenario: SNI mismatch
- **WHEN** a client sends `CONNECT pypi.org:443` and then a ClientHello with SNI `evil.example`
- **THEN** the sidecar terminates the connection

#### Scenario: Raw tunnel
- **WHEN** a client opens an allowed CONNECT tunnel and sends non-TLS bytes
- **THEN** the sidecar terminates the connection

#### Scenario: Metadata address
- **WHEN** a client requests `169.254.169.254` by IP, or by an allowlisted name resolving to it in direct mode
- **THEN** the sidecar denies the request without opening an upstream connection; the IP-literal request is closed without `200`

#### Scenario: Mixed public and private answers
- **WHEN** an allowlisted name resolves to both a public and a private address in direct mode
- **THEN** the request is denied with cause `private-address`

### Requirement: GitHub repository access is blocked in sidecar mode
The sidecar SHALL NOT admit the listed GitHub repository hosts (`github.com`, `api.github.com`, `uploads.github.com`, `codeload.github.com`, `gist.github.com`, `lfs.github.com`, `raw.githubusercontent.com`, `gist.githubusercontent.com`, `media.githubusercontent.com`, `objects.githubusercontent.com`, `objects-origin.githubusercontent.com`, `github-cloud.githubusercontent.com`, `github-releases.githubusercontent.com`, `release-assets.githubusercontent.com`, `pkg-containers.githubusercontent.com`), except `api.github.com` restricted to the Copilot paths. The system SHALL reject these hosts in the policy file's `allow` and in `project-sandbox-egress allow add`, including through a covering wildcard. It SHALL reject `--allow-github` and an `[egress.github]` policy table in sidecar mode. The documentation SHALL state that this blocks the listed hosts, and that repository content served from other domains is not classified.

#### Scenario: Clone is denied
- **WHEN** the agent runs `git clone https://github.com/acme/tool` in a sidecar session
- **THEN** the connection is denied and the denial is logged with its cause

#### Scenario: LFS is denied
- **WHEN** a client connects to `lfs.github.com`
- **THEN** the connection is denied

#### Scenario: Repository API is denied with Copilot enabled
- **WHEN** the Copilot profile intercepts `api.github.com` and the agent requests `https://api.github.com/repos/acme/tool`
- **THEN** the request is denied with a labelled 403

#### Scenario: Wildcard cannot re-admit GitHub
- **WHEN** the policy file allows `*.githubusercontent.com`, or the user runs `project-sandbox-egress allow add github.com`
- **THEN** the system exits with an error naming the GitHub repository hosts it would admit

#### Scenario: --allow-github in sidecar mode
- **WHEN** `--egress sidecar` is combined with `--allow-github`
- **THEN** the CLI exits with an error before any network, key, or container work

### Requirement: Copilot access is a separate host profile
The policy SHALL offer a Copilot profile, enabled only by `[egress.copilot] enabled = true` in `egress.toml`, that admits the GitHub Copilot service hosts without granting repository access. A sidecar-mode launch with `--agent copilot` SHALL fail before the sandbox starts when the running or configured sidecar does not have the profile enabled, naming the key to set. The profile SHALL NOT admit Copilot telemetry hosts. The Copilot service hosts SHALL be spliced. The profile SHALL admit `api.github.com` only through interception, and only for the exact method and path pairs recorded by the `copilot-egress` capture. It SHALL deny intercepted requests whose raw path contains a dot segment or a percent-encoded `.`, `/` or `\`, and requests whose `Host` header differs from the CONNECT host. The repository SHALL provide an opt-in, billable `copilot-egress` e2e suite that records the Copilot CLI's hosts, methods and paths and verifies the profile.

#### Scenario: Copilot without repositories
- **WHEN** the Copilot profile is enabled
- **THEN** a Copilot CLI session succeeds and `git clone https://github.com/acme/tool` is denied

#### Scenario: Copilot agent without the profile
- **WHEN** `--agent copilot --egress sidecar` is launched and `egress.toml` does not enable the Copilot profile
- **THEN** the launch fails before the sandbox starts and names `[egress.copilot] enabled = true`

#### Scenario: Copilot telemetry is blocked
- **WHEN** the Copilot CLI posts telemetry to `telemetry.individual.githubcopilot.com` in a sidecar session
- **THEN** the connection is denied and the Copilot session still succeeds

#### Scenario: Unrecorded path or method
- **WHEN** a client sends `POST /copilot_internal/user` or `GET /copilot_internal/other` to `api.github.com`
- **THEN** the request is denied with cause `path-not-allowed`

#### Scenario: Path tricks
- **WHEN** a client requests `/copilot_internal/../repos/acme/tool`, `/copilot_internal/%2e%2e/repos/acme/tool` or `/copilot_internal%2fuser` on `api.github.com`
- **THEN** each request is denied

#### Scenario: Conflicting Host header
- **WHEN** a client sends an intercepted request to `api.github.com` with `Host: uploads.github.com`
- **THEN** the request is denied

#### Scenario: Profile verification
- **WHEN** the `copilot-egress` suite runs
- **THEN** it reports the hosts, methods and paths Copilot used, whether the profile works without `api.github.com`, and that every GitHub repository probe was denied

### Requirement: Policy lives on the host outside the sandbox's reach
The system SHALL read sidecar policy only from `$XDG_CONFIG_HOME/project-sandbox/egress.toml`; there SHALL be no per-launch policy flag. The effective allowlist SHALL be the policy's `allow` entries plus the provider hosts selected by `[egress] providers`: every agent's provider hosts when it is `"all"` or absent, the listed agents' provider hosts for a list of agent names, and none for an empty list. A sidecar-mode launch of an agent whose provider hosts are not selected SHALL fail before the sandbox starts, unless the session uses an agent proxy. A sidecar-mode launch SHALL be rejected when the policy file or the sidecar state directory resolves inside the project, the worktree, the main repository's `.git`, or any other directory mounted into the sandbox. A launch in any egress mode SHALL be rejected when any mount source, in any mount form, resolves to, inside, or around the policy directory or the sidecar state directory. The system SHALL reject unknown keys and invalid hostnames. Rendered sidecar config SHALL be written to the sidecar state directory, not to the project.

#### Scenario: Policy inside the project
- **WHEN** a sidecar-mode launch mounts a directory that contains `egress.toml` or the sidecar state directory
- **THEN** the CLI exits with an error explaining that the agent could modify the policy or read the CA key

#### Scenario: Provider hosts are always allowed
- **WHEN** `egress.toml` allows only `pypi.org` and does not set `providers`
- **THEN** the sidecar admits `pypi.org` and the provider hosts of every supported agent

#### Scenario: No implicit providers
- **WHEN** `egress.toml` sets `providers = []` and allows only `pypi.org`
- **THEN** the sidecar admits only `pypi.org`, and a sidecar-mode `--agent claude` launch without an agent proxy fails, naming `providers`

#### Scenario: Selected providers
- **WHEN** `egress.toml` sets `providers = ["claude"]`
- **THEN** the sidecar admits the Claude provider hosts and no OpenAI host

#### Scenario: Effective policy preview
- **WHEN** the user runs with `--egress sidecar --dry-run`
- **THEN** the output shows the policy path, the effective allowlist, the Copilot profile, intercepted hosts, CA constraints, the published endpoint and whether the sidecar would be started or reused, and no file, key, or container is created

#### Scenario: Unknown key
- **WHEN** the policy file contains an unrecognized key
- **THEN** the CLI exits with an error naming the key

#### Scenario: Mount inside the sidecar state directory
- **WHEN** a launch in any egress mode mounts a subdirectory of the sidecar state directory, for example the one holding the CA key
- **THEN** the CLI exits with an error naming the protected location

#### Scenario: Unmodeled mount option
- **WHEN** a mount of the policy directory carries an extra option such as `bind-propagation=rshared`
- **THEN** the mount is rejected and never passed to the runtime

### Requirement: Denials are observable
Local denials SHALL be labelled distinctly per cause, as a labelled response page for plain HTTP and intercepted requests and as an access-log cause annotation for CONNECT-level and TLS-level denials: not allowlisted, plain HTTP not allowed, SNI mismatch, non-TLS tunnel, IP literal, port, private or metadata address, and path not allowed on an intercepted host. At session end, the CLI SHALL print the number of requests the sidecar denied between the session's start and end, across all of its clients, with the `project-sandbox-egress log` command that lists them.

#### Scenario: Blocked host summary
- **WHEN** the sidecar denied three requests while a session ran
- **THEN** the session-end output reports 3 denials during the session across all sandboxes and prints `project-sandbox-egress log --denied --since <session start>`

#### Scenario: No denials
- **WHEN** the sidecar denied nothing during the session
- **THEN** the session-end output prints no denial summary

#### Scenario: Plain HTTP denial
- **WHEN** a plain HTTP request to an allowlisted host outside `http_allow` is denied
- **THEN** the response page and the access log name the cause `plain-http-not-allowed`

## ADDED Requirements

### Requirement: Tunnel lifetime is bounded
The sidecar SHALL close any client connection, including spliced tunnels, after a configurable maximum lifetime, `[egress.sidecar] tunnel_lifetime`, defaulting to one hour. Reloading the allowlist SHALL apply to new connections only.

#### Scenario: Long-lived tunnel
- **WHEN** a spliced tunnel stays open longer than `tunnel_lifetime`
- **THEN** the sidecar closes it, and the client's next connection is checked against the current allowlist

#### Scenario: Revocation of a host with an open tunnel
- **WHEN** a host is removed while a tunnel to it is open
- **THEN** new connections to the host are denied immediately, and the open tunnel closes at the latest when its lifetime expires

### Requirement: Upstream mode states the parent's responsibility
When a parent proxy is configured, `project-sandbox-egress status`, every sidecar-mode launch and `project-sandbox doctor` SHALL state that the parent must enforce private-address and cloud-metadata restrictions, because the sidecar does not resolve destinations in that mode.

#### Scenario: Launch with a parent
- **WHEN** a sidecar-mode session starts with `[egress.upstream] url` set
- **THEN** the launch output includes the parent-responsibility notice

### Requirement: Sidecar logs are bounded and redacted
The sidecar's access log SHALL NOT contain URL query strings or request headers. On runtimes that support log rotation options, the sidecar container SHALL run with bounded log size and file count.

#### Scenario: Query string in an intercepted request
- **WHEN** an intercepted request to `api.github.com` carries a query string
- **THEN** the access log line for it contains the path without the query string

#### Scenario: Docker log rotation
- **WHEN** the sidecar starts on Docker or Podman
- **THEN** its run command sets a maximum log size and file count
