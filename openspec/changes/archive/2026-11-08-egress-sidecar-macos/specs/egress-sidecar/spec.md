# Spec Delta

## Purpose

The opt-in `--egress sidecar` mode routes a sandbox's Internet traffic through one global Squid sidecar per runtime that filters by hostname and TLS SNI instead of pinned IP addresses. It blocks GitHub repository access, admits Copilot through a narrow profile, and can chain to an upstream parent proxy.

## ADDED Requirements

### Requirement: Sidecar egress is an explicit opt-in mode
The CLI SHALL accept `--egress firewall|sidecar`, defaulting to `firewall`. With `firewall`, egress behaviour SHALL be unchanged. The CLI SHALL reject `--egress sidecar` before any network, key generation, or container work when combined with `--no-firewall`, `--runtime chroot`, `--extra-domain`, `--internet-proxy`, `--ca-cert`, `--allow-github` or `--copilot-host`. Each rejection SHALL name the `egress.toml` key, `project-sandbox-egress` command or egress mode to use instead. Sidecar mode SHALL be supported on Apple `container`, Docker Desktop and Podman machine; on Docker Engine and rootless Podman on a Linux host it SHALL be rejected before any container work, naming the supported runtimes. Devcontainers remain firewall-only: a sidecar-mode launch SHALL NOT render or modify `.devcontainer/` and SHALL say so.

#### Scenario: Default mode is unchanged
- **WHEN** the user omits `--egress`
- **THEN** the rendered firewall, run argv, and environment are identical to those produced before this change

#### Scenario: Sidecar mode is selected
- **WHEN** the user runs with `--egress sidecar --runtime docker`
- **THEN** the CLI plans the sandbox behind the runtime's global sidecar, starting that sidecar first if it is not running

#### Scenario: Unsupported combination
- **WHEN** `--egress sidecar` is combined with `--no-firewall` or `--runtime chroot`
- **THEN** the CLI exits with an error naming the conflict and performs no network, key, or container work

#### Scenario: Linux runtime
- **WHEN** `--egress sidecar` is used on Docker Engine or rootless Podman on a Linux host
- **THEN** the CLI exits with an error naming Apple `container`, Docker Desktop and Podman machine, and performs no container work

#### Scenario: Per-launch policy flags are rejected
- **WHEN** `--egress sidecar` is combined with `--extra-domain pypi.org`, `--internet-proxy URL` or `--ca-cert PATH`
- **THEN** the CLI exits with an error naming `project-sandbox-egress allow add` or the `egress.toml` upstream keys, and performs no network, key, or container work

#### Scenario: Devcontainer is not rendered in sidecar mode
- **WHEN** a sidecar-mode launch renders sandbox assets
- **THEN** `.devcontainer/` is neither created nor modified, and the CLI prints that devcontainers support firewall egress only

### Requirement: The sidecar's proxy endpoint is published on the host
The global sidecar SHALL publish its proxy port only on the host's loopback address, on the port set by `[egress.sidecar] port` in `egress.toml` (default 3129, range 1024–65535). Starting the sidecar SHALL fail before the container is created when that port is not free, naming the key. A sidecar-mode launch SHALL fail before the sandbox starts when the running sidecar publishes a different port than `egress.toml` configures, naming `project-sandbox-egress restart`.

#### Scenario: Default endpoint
- **WHEN** the sidecar starts and `egress.toml` sets no port
- **THEN** the proxy answers on `127.0.0.1:3129` on the host and on no other host address

#### Scenario: Port taken
- **WHEN** another process listens on `127.0.0.1:3129` and the sidecar is started
- **THEN** startup fails before any container is created and names `[egress.sidecar] port`

#### Scenario: Port changed while running
- **WHEN** `egress.toml` sets `port = 3140` while the running sidecar publishes 3129, and a sidecar-mode sandbox is launched
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress restart`

### Requirement: The sandbox reaches the Internet only through the sidecar
A sidecar-mode sandbox SHALL be run like a firewall-mode sandbox with an Internet proxy whose endpoint is the sidecar's published endpoint. Its firewall SHALL default-deny, SHALL admit new TCP connections only to the sidecar endpoint and to each local service or remote agent proxy this session uses, and SHALL admit no DNS. It SHALL fail closed when the required IPv6 policy cannot be enforced. Proxy environment variables SHALL point at the sidecar, and `NO_PROXY`/`no_proxy` SHALL keep the local-service bypass entries used in firewall mode.

#### Scenario: Direct egress is blocked
- **WHEN** a process in the sandbox connects directly to a public address or resolves a name through DNS, ignoring proxy variables
- **THEN** the connection or lookup fails

#### Scenario: Host services are not reachable
- **WHEN** a process in the sandbox connects to a host address on a port that is neither the sidecar's nor an enabled local service's
- **THEN** the sandbox firewall drops the connection

#### Scenario: Proxy environment targets the sidecar
- **WHEN** a sidecar-mode session starts with the default port
- **THEN** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` are `http://host.docker.internal:3129`, and `NO_PROXY`/`no_proxy` keep the local-service bypass entries used in firewall mode

#### Scenario: Two sandboxes share the sidecar
- **WHEN** two sidecar-mode sandboxes run at the same time on one runtime
- **THEN** both reach the Internet through the same sidecar, and neither launch fails because the other holds a host port

### Requirement: Local services reach sidecar-mode sandboxes as in firewall mode
In sidecar mode, the agent proxy and Ollama SHALL be forwarded into the sandbox with the same per-runtime local-service strategy as in firewall mode, and the sidecar SHALL NOT forward them. A remote `--agent-proxy` SHALL be admitted as in firewall mode with an Internet proxy: as a direct, port-scoped firewall destination.

#### Scenario: Loopback-only Ollama
- **WHEN** a sidecar-mode session with Ollama enabled starts and Ollama listens only on the host's `127.0.0.1:11434`
- **THEN** the sandbox reaches it at `host.docker.internal:11434` without going through the sidecar

#### Scenario: Remote agent proxy
- **WHEN** a sidecar-mode session starts with `--agent-proxy https://gateway.corp.example:8443`
- **THEN** the sandbox firewall admits that gateway's resolved addresses on port 8443, and the agent's model traffic does not pass through the sidecar

### Requirement: Destinations are filtered by hostname with SNI verification
The sidecar SHALL deny every request unless its destination hostname matches the effective allowlist (exact or `*.`-wildcard). It SHALL deny CONNECT to ports other than 443, IP-literal destinations, and (when no parent proxy is set) cloud-metadata and private, loopback, and link-local resolved addresses. All of these denials SHALL be issued before any upstream connection is made, and the sidecar SHALL record the denial cause in its access log. Port, IP-literal and allowlist denials SHALL close the client connection without responding `200 Connection established`. A resolved-address denial (which needs a DNS lookup) MAY answer `200` before the client's TLS handshake fails, but SHALL NOT open the upstream connection. For allowed CONNECTs, the sidecar SHALL peek at the TLS ClientHello and SHALL terminate the connection when the SNI is missing, is not allowlisted, or differs from the CONNECT host, or when the tunnel does not carry TLS.

#### Scenario: Unlisted host
- **WHEN** a client sends `CONNECT example.org:443` and `example.org` is not allowlisted
- **THEN** the sidecar closes the connection without responding `200 Connection established`, opens no upstream connection, and logs the cause `host-not-allowed`

#### Scenario: Wildcard host
- **WHEN** the allowlist contains `*.npmjs.org` and a client connects to `registry.npmjs.org`
- **THEN** the connection is allowed and spliced

#### Scenario: SNI mismatch
- **WHEN** a client sends `CONNECT pypi.org:443` and then a ClientHello with SNI `evil.example`
- **THEN** the sidecar terminates the connection

#### Scenario: Raw tunnel
- **WHEN** a client opens an allowed CONNECT tunnel and sends non-TLS bytes
- **THEN** the sidecar terminates the connection

#### Scenario: Metadata address
- **WHEN** a client requests `169.254.169.254` by IP, or by an allowlisted name resolving to it in direct mode
- **THEN** the sidecar denies the request without opening an upstream connection; the IP-literal request is closed without `200`

### Requirement: TLS interception is minimal and uses a name-constrained sidecar CA
The system SHALL intercept TLS at most for `api.github.com`, and only when the Copilot profile is enabled. It SHALL splice all other allowed hosts. The system SHALL generate the sidecar's CA on the host with `BasicConstraints(ca=True, pathlen=0)` and critical name constraints permitting the intercepted hosts' registrable domain, or only the reserved name `invalid` when nothing is intercepted. The CA's lifetime SHALL come from `egress.toml` (default 7 days, at most 30). The private key SHALL be stored mode 0600, mounted read-only only into the sidecar, and deleted when the sidecar is brought down or the CA is replaced. When interception is active, the public certificate SHALL be installed into each sandbox's system trust at container start, not in the image. Under `--dry-run`, the system SHALL NOT generate a key.

#### Scenario: Nothing intercepted means no trusted CA
- **WHEN** the sidecar intercepts no host
- **THEN** the CA is constrained to `invalid`, no CA certificate is mounted into the sandbox, and every allowed host is spliced

#### Scenario: CA is constrained
- **WHEN** `api.github.com` is intercepted for the Copilot token exchange
- **THEN** the generated CA certificate carries a critical name constraint permitting only `github.com` and its subdomains

#### Scenario: Key stays out of the sandbox
- **WHEN** a sandbox starts while interception is active
- **THEN** the sandbox run argv contains a mount for the public certificate only, and the private key is mounted only into the sidecar

#### Scenario: Image cache is unaffected
- **WHEN** the sidecar's CA is replaced between two sessions
- **THEN** the sandbox image checksum is the same for both

#### Scenario: Spliced hosts are untouched
- **WHEN** a client connects to an allowed host other than an intercepted one
- **THEN** the client receives the origin's (or upstream proxy's) certificate, not one minted by the sidecar CA

#### Scenario: Lifetime is capped
- **WHEN** `egress.toml` sets `[egress.ca] lifetime_days = 90`
- **THEN** the policy is rejected with an error stating the 30-day maximum

### Requirement: GitHub repository access is blocked in sidecar mode
The sidecar SHALL NOT admit GitHub repository hosts (`github.com`, `api.github.com`, `uploads.github.com`, `codeload.github.com`, `gist.github.com`, `raw.githubusercontent.com`, `gist.githubusercontent.com`, `media.githubusercontent.com`, `objects.githubusercontent.com`, `github-cloud.githubusercontent.com`, `release-assets.githubusercontent.com`), except `api.github.com` restricted to the Copilot token-exchange paths. The system SHALL reject these hosts in the policy file's `allow` and in `project-sandbox-egress allow add`, including through a covering wildcard. It SHALL reject `--allow-github` and an `[egress.github]` policy table in sidecar mode, pointing to firewall mode and the roadmap.

#### Scenario: Clone is denied
- **WHEN** the agent runs `git clone https://github.com/acme/tool` in a sidecar session
- **THEN** the connection is denied and the denial is logged with its cause

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
The policy SHALL offer a Copilot profile, enabled only by `[egress.copilot] enabled = true` in `egress.toml`, that admits the GitHub Copilot service hosts without granting repository access. A sidecar-mode launch with `--agent copilot` SHALL fail before the sandbox starts when the running or configured sidecar does not have the profile enabled, naming the key to set. The profile SHALL NOT admit Copilot telemetry hosts. The Copilot service hosts SHALL be spliced. The profile SHALL admit `api.github.com` only through interception with an allowlist of Copilot token-exchange paths. The repository SHALL provide an opt-in, billable `copilot-egress` e2e suite that records the Copilot CLI's hosts and paths and verifies the profile.

#### Scenario: Copilot without repositories
- **WHEN** the Copilot profile is enabled
- **THEN** a Copilot CLI session succeeds and `git clone https://github.com/acme/tool` is denied

#### Scenario: Copilot agent without the profile
- **WHEN** `--agent copilot --egress sidecar` is launched and `egress.toml` does not enable the Copilot profile
- **THEN** the launch fails before the sandbox starts and names `[egress.copilot] enabled = true`

#### Scenario: Copilot telemetry is blocked
- **WHEN** the Copilot CLI posts telemetry to `telemetry.individual.githubcopilot.com` in a sidecar session
- **THEN** the connection is denied and the Copilot session still succeeds

#### Scenario: Profile verification
- **WHEN** the `copilot-egress` suite runs
- **THEN** it reports the hosts and paths Copilot used, whether the profile works without `api.github.com`, and that every GitHub repository probe was denied

### Requirement: Policy lives on the host outside the sandbox's reach
The system SHALL read sidecar policy only from `$XDG_CONFIG_HOME/project-sandbox/egress.toml`; there SHALL be no per-launch policy flag. The effective allowlist SHALL be the policy's `allow` entries plus the provider hosts of every supported agent. A sidecar-mode launch SHALL be rejected when the policy file or the sidecar state directory resolves inside the project, the worktree, the main repository's `.git`, or any other directory mounted into the sandbox. The system SHALL reject unknown keys and invalid hostnames. Rendered sidecar config SHALL be written to the sidecar state directory, not to the project.

#### Scenario: Policy inside the project
- **WHEN** a sidecar-mode launch mounts a directory that contains `egress.toml` or the sidecar state directory
- **THEN** the CLI exits with an error explaining that the agent could modify the policy or read the CA key

#### Scenario: Provider hosts are always allowed
- **WHEN** `egress.toml` allows only `pypi.org`
- **THEN** the sidecar admits `pypi.org` and the provider hosts of every supported agent

#### Scenario: Effective policy preview
- **WHEN** the user runs with `--egress sidecar --dry-run`
- **THEN** the output shows the policy path, the effective allowlist, the Copilot profile, intercepted hosts, CA constraints, the published endpoint and whether the sidecar would be started or reused, and no file, key, or container is created

#### Scenario: Unknown key
- **WHEN** the policy file contains an unrecognized key
- **THEN** the CLI exits with an error naming the key

### Requirement: The sidecar can chain to an upstream parent proxy
`[egress.upstream] url` in `egress.toml` SHALL configure the sidecar to forward all traffic through that proxy as its only parent, never going direct. Its `ca_cert` files SHALL be trusted by the sidecar for upstream TLS and installed into each sandbox's system trust at container start. In parent mode, local policy SHALL be hostname-based and SHALL NOT require local DNS resolution of destinations. A parent on host loopback SHALL be reachable from the sidecar. On Apple `container`, the sidecar SHALL reach a remote parent through a host relay that the sidecar owns. Upstream proxy authentication is not supported, and HTTP 407 SHALL fail the request.

#### Scenario: Corporate parent on Docker
- **WHEN** `egress.toml` sets `[egress.upstream] url = "https://proxy.corp.example"` and `ca_cert = ["corp.pem"]` and a sidecar starts with `--runtime docker`
- **THEN** the rendered Squid config declares that proxy as the sole parent with TLS verified against system trust plus `corp.pem`, and contains no rule permitting direct forwarding

#### Scenario: Corporate parent on Apple container
- **WHEN** the same configuration is used with `--runtime apple-container`
- **THEN** the sidecar's parent is the sidecar-owned host relay on `host.docker.internal`, and concurrent sandboxes all reach the parent through it

#### Scenario: Corporate interception of a spliced host
- **WHEN** the parent intercepts TLS for an allowed, spliced host
- **THEN** the sandbox client validates the parent-minted certificate through the `ca_cert` trust installed at container start

#### Scenario: Denial attribution
- **WHEN** a request is denied by the parent proxy rather than local policy
- **THEN** `project-sandbox-egress log --denied` attributes the denial to the upstream proxy

### Requirement: One global sidecar per runtime is started on demand
Each runtime SHALL have at most one egress sidecar. A sidecar-mode launch SHALL reuse the sidecar when it is running and answers its readiness probe. Otherwise the launch SHALL start it from `egress.toml`, replacing a stopped or unready container, and SHALL wait within a bounded timeout. Concurrent launches SHALL serialize so that only one starts the sidecar. A session's end SHALL NOT stop the sidecar.

#### Scenario: First launch starts the sidecar
- **WHEN** no sidecar exists and a sidecar-mode sandbox is launched
- **THEN** the sidecar is started and becomes ready before the sandbox starts

#### Scenario: Later launches reuse it
- **WHEN** the sidecar is running and ready and a second sandbox is launched
- **THEN** no sidecar is created, and the sandbox uses the running sidecar

#### Scenario: Concurrent first launches
- **WHEN** two sidecar-mode launches start at the same time and no sidecar exists
- **THEN** exactly one sidecar is created and both sandboxes use it

#### Scenario: Sidecar fails to become ready
- **WHEN** the sidecar does not answer within the readiness timeout
- **THEN** the sandbox is not started and the CLI reports the sidecar log tail

#### Scenario: Normal exit
- **WHEN** a sidecar-mode sandbox exits
- **THEN** the sidecar keeps running, and the session's own sandbox inputs are removed

#### Scenario: Stopped sidecar after a reboot
- **WHEN** the sidecar container exists but is stopped
- **THEN** the next sidecar-mode launch replaces it and the sandbox starts

### Requirement: Launches reconcile configuration drift
Before starting a sandbox, a sidecar-mode launch SHALL compare `egress.toml` and the sidecar image with what the running sidecar uses. Allowlist differences SHALL be applied to the running sidecar without a restart. Differences that need a restart SHALL produce a warning naming each field and `project-sandbox-egress restart`, and the launch SHALL continue on the running sidecar, except for the published port (see "The sidecar's proxy endpoint is published on the host"). These are the upstream, the Copilot profile, the port, the resource limits and the image.

#### Scenario: Hand-edited allowlist
- **WHEN** the user added a host to `egress.toml` by hand while the sidecar was running
- **THEN** the next sidecar-mode launch applies it to the running sidecar without restarting it

#### Scenario: Upgraded image
- **WHEN** the running sidecar uses an older sidecar image than the installed version would build
- **THEN** the launch warns that the sidecar is stale and names `project-sandbox-egress restart`

### Requirement: Launches warn about an expiring CA
When interception is active, a sidecar-mode launch SHALL warn when the sidecar's CA expires within 2 days, naming `project-sandbox-egress restart`. It SHALL refuse to start the sandbox when the CA has expired. When nothing is intercepted, launches SHALL NOT warn about CA expiry.

#### Scenario: CA close to expiry
- **WHEN** interception is active and the CA expires in 30 hours
- **THEN** the launch prints a warning naming `project-sandbox-egress restart` and starts the sandbox

#### Scenario: CA expired
- **WHEN** interception is active and the CA has expired
- **THEN** the launch exits with an error before starting the sandbox

### Requirement: The host relay is supervised by the sidecar
When the configured upstream needs a host relay (Apple `container` with a remote parent), the relay SHALL be a host process owned by the sidecar, not by any sandbox's CLI. It SHALL outlive the launching CLI and stop with `project-sandbox-egress down`. A launch SHALL restart a relay that is no longer running. If the old port is taken, the relay SHALL move to a new port and the sidecar SHALL follow without a restart.

#### Scenario: Relay outlives the first session
- **WHEN** the session that started the sidecar exits while another sandbox is running
- **THEN** the other sandbox still reaches the upstream proxy

#### Scenario: Relay died
- **WHEN** the relay process was killed and a sidecar-mode sandbox is launched
- **THEN** the launch restarts the relay before starting the sandbox

### Requirement: Denials are observable
Local denials SHALL be labelled distinctly per cause, as a labelled response page for plain HTTP and intercepted requests and as an access-log cause annotation for CONNECT-level and TLS-level denials: not allowlisted, SNI mismatch, non-TLS tunnel, IP literal, port, private or metadata address, and path not allowed on an intercepted host. At session end, the CLI SHALL print the number of requests the sidecar denied between the session's start and end, across all of its clients, with the `project-sandbox-egress log` command that lists them.

#### Scenario: Blocked host summary
- **WHEN** the sidecar denied three requests while a session ran
- **THEN** the session-end output reports 3 denials during the session across all sandboxes and prints `project-sandbox-egress log --denied --since <session start>`

#### Scenario: No denials
- **WHEN** the sidecar denied nothing during the session
- **THEN** the session-end output prints no denial summary

### Requirement: The sidecar runs with resource limits
The sidecar container SHALL run with the memory and CPU limits from `[egress.sidecar]` in `egress.toml`, with defaults when they are not set. On Docker and Podman, its runtime log SHALL be size-capped.

#### Scenario: Default limits
- **WHEN** `egress.toml` sets no sidecar limits
- **THEN** the sidecar run argv contains the default `--memory` and `--cpus` values

#### Scenario: Configured limits
- **WHEN** `egress.toml` sets `[egress.sidecar] memory = "512M"` and `cpus = 2`
- **THEN** the sidecar run argv contains `--memory 512M` and `--cpus 2`

### Requirement: Sidecar mode is verifiable end to end
The repository SHALL provide an opt-in `egress-sidecar` e2e suite. It SHALL cover direct-egress denial, unlisted host, SNI mismatch, raw tunnel, metadata, allowed splice, GitHub repository access denied, parent-proxy chaining with an intercepting fixture proxy, and a local service listening only on host loopback. It SHALL also cover two concurrent sandboxes on one sidecar, reuse across launches, a live `allow add`, and `down` while a sandbox runs. The suite SHALL run on Apple `container`, Docker Desktop and Podman machine. Unit tests SHALL NOT require a container runtime.

#### Scenario: Suite is opt-in
- **WHEN** `scripts/run-e2e-tests.sh` runs without selecting the suite
- **THEN** the `egress-sidecar` suite is skipped

#### Scenario: Shared sidecar coverage
- **WHEN** the `egress-sidecar` suite runs
- **THEN** it starts two sandboxes concurrently, verifies that both use one sidecar, that an `allow add` reaches both without a restart, and that `down` makes both lose egress
