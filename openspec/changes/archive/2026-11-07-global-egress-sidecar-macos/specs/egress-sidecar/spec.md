# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: TLS interception is minimal and uses a per-session name-constrained CA`
- TO: `### Requirement: TLS interception is minimal and uses a name-constrained sidecar CA`

- FROM: `### Requirement: Sidecar lifecycle is managed and leaves no residue`
- TO: `### Requirement: One global sidecar per runtime is started on demand`

## MODIFIED Requirements

### Requirement: Sidecar egress is an explicit opt-in mode
The CLI SHALL accept `--egress firewall|sidecar`, defaulting to `firewall`. With `firewall`, egress behaviour SHALL be unchanged. The CLI SHALL reject `--egress sidecar` before any network, key generation, or container work when combined with `--no-firewall`, chroot, `--extra-domain`, `--internet-proxy` or `--ca-cert`. Each rejection SHALL name the `egress.toml` key or `project-sandbox-egress` command to use instead. Devcontainers remain firewall-only: a sidecar-mode launch SHALL NOT render or modify `.devcontainer/` and SHALL say so.

#### Scenario: Default mode is unchanged
- **WHEN** the user omits `--egress`
- **THEN** the rendered firewall, run argv, and environment are identical to those produced before this change

#### Scenario: Sidecar mode is selected
- **WHEN** the user runs with `--egress sidecar --runtime docker`
- **THEN** the CLI plans the sandbox on the runtime's global internal network behind the global sidecar, starting that sidecar first if it is not running

#### Scenario: Unsupported combination
- **WHEN** `--egress sidecar` is combined with `--no-firewall` or `--runtime chroot`
- **THEN** the CLI exits with an error naming the conflict and performs no network, key, or container work

#### Scenario: Per-launch policy flags are rejected
- **WHEN** `--egress sidecar` is combined with `--extra-domain pypi.org`, `--internet-proxy URL` or `--ca-cert PATH`
- **THEN** the CLI exits with an error naming `project-sandbox-egress allow add` or the `egress.toml` upstream keys, and performs no network, key, or container work

#### Scenario: Devcontainer is not rendered in sidecar mode
- **WHEN** a sidecar-mode launch renders sandbox assets
- **THEN** `.devcontainer/` is neither created nor modified, and the CLI prints that devcontainers support firewall egress only

### Requirement: The sandbox reaches the Internet only through its sidecar
In sidecar mode, every sandbox of a runtime SHALL be attached only to that runtime's global internal network, and the global sidecar SHALL be attached to both that network and the runtime's egress network. The sandbox firewall SHALL default-deny and SHALL admit only TCP to the sidecar's proxy port and to the sidecar on each local-service port that this session uses. It SHALL admit no DNS and no host address. The sidecar SHALL forward each configured local-service port to the host-local service, accepting connections only from the internal subnet, and the sandbox SHALL resolve `host.docker.internal` to the sidecar. Proxy environment variables SHALL point at the sidecar.

#### Scenario: Direct egress is blocked
- **WHEN** a process in the sandbox connects directly to a public address, ignoring proxy variables
- **THEN** the connection fails

#### Scenario: Host services are not reachable
- **WHEN** a process in the sandbox connects to a host address on a port that is not an enabled local service
- **THEN** the sandbox firewall drops the connection

#### Scenario: Proxy environment targets the sidecar
- **WHEN** a sidecar-mode session starts
- **THEN** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` name the sidecar, and `NO_PROXY`/`no_proxy` keep the local-service bypass entries used in firewall mode

#### Scenario: Local services are forwarded by the sidecar
- **WHEN** a sidecar-mode session with Ollama or the agent proxy enabled starts and the sidecar forwards that port
- **THEN** the sandbox reaches `host.docker.internal:<port>` through the sidecar's forwarder, and the sandbox firewall admits that port only on the sidecar's internal IP

#### Scenario: Another session's forwarder is closed
- **WHEN** the sidecar forwards ports 8787 and 11434 and a session uses only the agent proxy on 8787
- **THEN** that sandbox's firewall admits 8787 and drops connections to the sidecar on 11434

#### Scenario: Port not forwarded
- **WHEN** a session needs local-service port 8787 and the sidecar's configuration does not forward it
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress forward add 8787`

#### Scenario: Sandboxes share the internal network
- **WHEN** two sidecar-mode sandboxes run on one runtime
- **THEN** both are attached to the same internal network, and neither can open a connection to the other

#### Scenario: Apple container topology
- **WHEN** a sidecar-mode session starts on `--runtime apple-container`
- **THEN** the run argv attaches the sandbox only to the global network, which was created with `container network create --internal`

### Requirement: TLS interception is minimal and uses a name-constrained sidecar CA
The system SHALL intercept TLS at most for `api.github.com`, and only when the Copilot profile is enabled and that host is handled in path-restricted mode. It SHALL splice all other allowed hosts. The system SHALL generate the sidecar's CA on the host with `BasicConstraints(ca=True, pathlen=0)` and critical name constraints permitting the intercepted hosts' registrable domain, or only the reserved name `invalid` when nothing is intercepted. The CA's lifetime SHALL come from `egress.toml` (default 7 days, at most 30). The private key SHALL be stored mode 0600, mounted read-only only into the sidecar, and deleted when the sidecar is brought down or the CA is replaced. When interception is active, the public certificate SHALL be installed into each sandbox's system trust at container start, not in the image. Under `--dry-run`, the system SHALL NOT generate a key.

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
The policy SHALL offer a Copilot profile, enabled only by `[egress.copilot] enabled = true` in `egress.toml`, that admits the GitHub Copilot service hosts without granting repository access. A sidecar-mode launch with `--agent copilot` SHALL fail before the sandbox starts when the running or configured sidecar does not have the profile enabled, naming the key to set. The profile SHALL NOT admit Copilot telemetry hosts. The Copilot service hosts SHALL be spliced. If the Copilot CLI needs `api.github.com`, the profile SHALL admit it only through interception with an allowlist of Copilot token-exchange paths. The repository SHALL provide an opt-in, billable `copilot-egress` e2e suite that records the Copilot CLI's hosts and paths and verifies the profile.

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
- **THEN** the output shows the policy path, the effective allowlist, the Copilot profile, intercepted hosts, CA constraints and whether the sidecar would be started or reused, and no file, key, network, or container is created

#### Scenario: Unknown key
- **WHEN** the policy file contains an unrecognized key
- **THEN** the CLI exits with an error naming the key

#### Scenario: Policy flag is gone
- **WHEN** the user passes `--egress-policy PATH`
- **THEN** argument parsing fails because the option does not exist

### Requirement: The sidecar can chain to an upstream parent proxy
`[egress.upstream] url` in `egress.toml` SHALL configure the sidecar to forward all traffic through that proxy as its only parent, never going direct. Its `ca_cert` files SHALL be trusted by the sidecar for upstream TLS and installed into each sandbox's system trust at container start. In parent mode, local policy SHALL be hostname-based and SHALL NOT require local DNS resolution of destinations. On Apple `container`, the sidecar SHALL reach a remote parent through a host relay that the sidecar owns. Upstream proxy authentication is not supported, and HTTP 407 SHALL fail the request.

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
Each runtime SHALL have at most one egress sidecar and one internal network for it. A sidecar-mode launch SHALL reuse the sidecar when it is running and answers its readiness probe. Otherwise the launch SHALL start it from `egress.toml`, replacing a stopped or unready container, and SHALL wait within a bounded timeout. Concurrent launches SHALL serialize so that only one starts the sidecar. A session's end SHALL NOT stop the sidecar. Labelled resources from per-session sidecars SHALL be removed when no session holds their lock.

#### Scenario: First launch starts the sidecar
- **WHEN** no sidecar exists and a sidecar-mode sandbox is launched
- **THEN** the sidecar is started and becomes ready before the sandbox starts

#### Scenario: Later launches reuse it
- **WHEN** the sidecar is running and ready and a second sandbox is launched
- **THEN** no sidecar or network is created, and the sandbox uses the running sidecar

#### Scenario: Concurrent first launches
- **WHEN** two sidecar-mode launches start at the same time and no sidecar exists
- **THEN** exactly one sidecar is created and both sandboxes use it

#### Scenario: Sidecar fails to become ready
- **WHEN** the sidecar does not answer within the readiness timeout
- **THEN** the sandbox is not started and the CLI reports the sidecar log tail

#### Scenario: Normal exit
- **WHEN** a sidecar-mode sandbox exits
- **THEN** the sidecar and its network keep running, and the session's own sandbox inputs are removed

#### Scenario: Stopped sidecar after a reboot
- **WHEN** the sidecar container exists but is stopped
- **THEN** the next sidecar-mode launch replaces it and the sandbox starts

#### Scenario: Orphan from a crash
- **WHEN** a per-session sidecar and network from an earlier version are still present and no session holds their lock
- **THEN** the next sidecar-mode launch or `project-sandbox-egress up` removes them

### Requirement: Denials are observable
Local denials SHALL be labelled distinctly per cause, as a labelled response page for plain HTTP and intercepted requests and as an access-log cause annotation for CONNECT-level and TLS-level denials: not allowlisted, SNI mismatch, non-TLS tunnel, IP literal, port, private or metadata address, and path not allowed on an intercepted host. Each access-log line SHALL include the client IP. At session end, the CLI SHALL print the number of requests the sidecar denied between the session's start and end, across all sandboxes, with the `project-sandbox-egress log` command that lists them.

#### Scenario: Blocked host summary
- **WHEN** the sidecar denied three requests while a session ran
- **THEN** the session-end output reports 3 denials during the session across all sandboxes and prints `project-sandbox-egress log --denied --since <session start>`

#### Scenario: No denials
- **WHEN** the sidecar denied nothing during the session
- **THEN** the session-end output prints no denial summary

### Requirement: Sidecar mode is verifiable end to end
The repository SHALL provide an opt-in `egress-sidecar` e2e suite. It SHALL cover direct-egress denial, unlisted host, SNI mismatch, raw tunnel, metadata, allowed splice, GitHub repository access denied, a Copilot session through the profile, and parent-proxy chaining with an intercepting fixture proxy. It SHALL also cover two concurrent sandboxes on one sidecar, reuse across launches, a live `allow add`, and `down` while a sandbox runs. Unit tests SHALL NOT require a container runtime.

#### Scenario: Suite is opt-in
- **WHEN** `scripts/run-e2e-tests.sh` runs without selecting the suite
- **THEN** the `egress-sidecar` suite is skipped

#### Scenario: Shared sidecar coverage
- **WHEN** the `egress-sidecar` suite runs
- **THEN** it starts two sandboxes concurrently, verifies that both use one sidecar, that an `allow add` reaches both without a restart, and that `down` makes both lose egress

## ADDED Requirements

### Requirement: Launches warn about an expiring CA
When interception is active, a sidecar-mode launch SHALL warn when the sidecar's CA expires within 2 days, naming `project-sandbox-egress restart`. It SHALL refuse to start the sandbox when the CA has expired. When nothing is intercepted, launches SHALL NOT warn about CA expiry.

#### Scenario: CA close to expiry
- **WHEN** interception is active and the CA expires in 30 hours
- **THEN** the launch prints a warning naming `project-sandbox-egress restart` and starts the sandbox

#### Scenario: CA expired
- **WHEN** interception is active and the CA has expired
- **THEN** the launch exits with an error before starting the sandbox

### Requirement: Launches reconcile configuration drift
Before starting a sandbox, a sidecar-mode launch SHALL compare `egress.toml` and the sidecar image with what the running sidecar uses. Allowlist and forwarder differences SHALL be applied to the running sidecar without a restart. Differences that need a restart SHALL produce a warning naming each field and `project-sandbox-egress restart`, and the launch SHALL continue on the running sidecar. These are the upstream, the Copilot profile, the resource limits and the image.

#### Scenario: Hand-edited allowlist
- **WHEN** the user added a host to `egress.toml` by hand while the sidecar was running
- **THEN** the next sidecar-mode launch applies it to the running sidecar without restarting it

#### Scenario: Upgraded image
- **WHEN** the running sidecar uses an older sidecar image than the installed version would build
- **THEN** the launch warns that the sidecar is stale and names `project-sandbox-egress restart`

### Requirement: The host relay is supervised by the sidecar
When the configured upstream needs a host relay (Apple `container` with a remote parent), the relay SHALL be a host process owned by the sidecar, not by any sandbox's CLI. It SHALL outlive the launching CLI and stop with `project-sandbox-egress down`. A launch SHALL restart a relay that is no longer running. If the old port is taken, the relay SHALL move to a new port and the sidecar SHALL follow without a restart.

#### Scenario: Relay outlives the first session
- **WHEN** the session that started the sidecar exits while another sandbox is running
- **THEN** the other sandbox still reaches the upstream proxy

#### Scenario: Relay died
- **WHEN** the relay process was killed and a sidecar-mode sandbox is launched
- **THEN** the launch restarts the relay before starting the sandbox

### Requirement: The sidecar runs with resource limits
The sidecar container SHALL run with the memory and CPU limits from `[egress.sidecar]` in `egress.toml`, with defaults when they are not set. On Docker and Podman, its runtime log SHALL be size-capped.

#### Scenario: Default limits
- **WHEN** `egress.toml` sets no sidecar limits
- **THEN** the sidecar run argv contains the default `--memory` and `--cpus` values

#### Scenario: Configured limits
- **WHEN** `egress.toml` sets `[egress.sidecar] memory = "512M"` and `cpus = 2`
- **THEN** the sidecar run argv contains `--memory 512M` and `--cpus 2`

### Requirement: Local-service forwarding needs a runtime-provided host alias
The sidecar SHALL forward local services only on runtimes where `host.docker.internal` reaches the host's loopback services from inside the sidecar: Apple `container` with the configured localhost alias, Docker Desktop, and Podman with a native host alias. On Docker Engine and on rootless Podman with pasta, starting a sidecar with configured forwarders SHALL fail and name the supported runtimes. Starting a sidecar without forwarders SHALL work there.

#### Scenario: Docker Desktop
- **WHEN** the sidecar starts on Docker Desktop with forwarded port 11434
- **THEN** the forwarder reaches the host's Ollama through `host.docker.internal`

#### Scenario: Docker Engine with forwarders
- **WHEN** the sidecar is to start on Docker Engine with a configured forwarder
- **THEN** startup fails before any container is created and explains that local-service forwarding on this runtime is not supported yet
