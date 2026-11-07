# Spec Delta

## MODIFIED Requirements

### Requirement: Sidecar egress is an explicit opt-in mode
The CLI SHALL accept `--egress firewall|sidecar`, defaulting to `firewall`. With `firewall`, egress behaviour SHALL be unchanged. The CLI SHALL reject `--egress sidecar` before any network, key generation, or container work when combined with `--no-firewall`, `--runtime chroot`, `--extra-domain`, `--internet-proxy`, `--ca-cert`, `--allow-github` or `--copilot-host`. Each rejection SHALL name the `egress.toml` key, `project-sandbox-egress` command or egress mode to use instead. Sidecar mode SHALL be supported on Apple `container`, Docker Desktop, Podman machine, Docker Engine and rootless Podman on a Linux host; on rootless Docker it SHALL be rejected before any container work, naming the supported runtimes. Devcontainers remain firewall-only: a sidecar-mode launch SHALL NOT render or modify `.devcontainer/` and SHALL say so.

#### Scenario: Default mode is unchanged
- **WHEN** the user omits `--egress`
- **THEN** the rendered firewall, run argv, and environment are identical to those produced before this change

#### Scenario: Sidecar mode is selected
- **WHEN** the user runs with `--egress sidecar --runtime docker`
- **THEN** the CLI plans the sandbox behind the runtime's global sidecar, starting that sidecar first if it is not running

#### Scenario: Unsupported combination
- **WHEN** `--egress sidecar` is combined with `--no-firewall` or `--runtime chroot`
- **THEN** the CLI exits with an error naming the conflict and performs no network, key, or container work

#### Scenario: Rootless Docker
- **WHEN** `--egress sidecar` is used with a rootless Docker daemon
- **THEN** the CLI exits with an error naming the supported runtimes, and performs no container work

#### Scenario: Per-launch policy flags are rejected
- **WHEN** `--egress sidecar` is combined with `--extra-domain pypi.org`, `--internet-proxy URL` or `--ca-cert PATH`
- **THEN** the CLI exits with an error naming `project-sandbox-egress allow add` or the `egress.toml` upstream keys, and performs no network, key, or container work

#### Scenario: Devcontainer is not rendered in sidecar mode
- **WHEN** a sidecar-mode launch renders sandbox assets
- **THEN** `.devcontainer/` is neither created nor modified, and the CLI prints that devcontainers support firewall egress only

### Requirement: The sidecar's proxy endpoint is published on the host
The global sidecar SHALL publish its proxy port on the port set by `[egress.sidecar] port` in `egress.toml` (default 3129, range 1024–65535), and on exactly one host address: the gateway address of Docker's default bridge on Docker Engine, and the host's loopback address on every other runtime. Starting the sidecar SHALL fail before the container is created when that address and port are not free, naming the key. A sidecar-mode launch SHALL fail before the sandbox starts when the running sidecar publishes a different address or port than the configuration and runtime require, naming `project-sandbox-egress restart`.

#### Scenario: Default endpoint
- **WHEN** the sidecar starts on Docker Desktop and `egress.toml` sets no port
- **THEN** the proxy answers on `127.0.0.1:3129` on the host and on no other host address

#### Scenario: Docker Engine endpoint
- **WHEN** the sidecar starts on Docker Engine, whose default bridge gateway is `172.17.0.1`
- **THEN** the proxy answers on `172.17.0.1:3129`, does not answer on `127.0.0.1:3129`, and two concurrent sidecar-mode sandboxes both reach it without any per-session listener for that port

#### Scenario: Rootless Podman endpoint
- **WHEN** the sidecar starts on rootless Podman with pasta
- **THEN** the proxy answers on `127.0.0.1:3129`, and each sidecar-mode sandbox reaches it through its pasta network, which forwards only the sidecar port and this session's local-service ports to host loopback

#### Scenario: Port taken
- **WHEN** another process listens on the sidecar's publish address and port and the sidecar is started
- **THEN** startup fails before any container is created and names `[egress.sidecar] port`

#### Scenario: Port changed while running
- **WHEN** `egress.toml` sets `port = 3140` while the running sidecar publishes 3129, and a sidecar-mode sandbox is launched
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress restart`

### Requirement: The sidecar can chain to an upstream parent proxy
`[egress.upstream] url` in `egress.toml` SHALL configure the sidecar to forward all traffic through that proxy as its only parent, never going direct. Its `ca_cert` files SHALL be trusted by the sidecar for upstream TLS and installed into each sandbox's system trust at container start. In parent mode, local policy SHALL be hostname-based and SHALL NOT require local DNS resolution of destinations. A parent on host loopback SHALL be reachable from the sidecar on every supported runtime, and SHALL NOT become reachable from sandboxes. On Apple `container`, the sidecar SHALL reach a remote parent through a host relay that the sidecar owns. Upstream proxy authentication is not supported, and HTTP 407 SHALL fail the request.

#### Scenario: Corporate parent on Docker
- **WHEN** `egress.toml` sets `[egress.upstream] url = "https://proxy.corp.example"` and `ca_cert = ["corp.pem"]` and a sidecar starts with `--runtime docker`
- **THEN** the rendered Squid config declares that proxy as the sole parent with TLS verified against system trust plus `corp.pem`, and contains no rule permitting direct forwarding

#### Scenario: Corporate parent on Apple container
- **WHEN** the same configuration is used with `--runtime apple-container`
- **THEN** the sidecar's parent is the sidecar-owned host relay on `host.docker.internal`, and concurrent sandboxes all reach the parent through it

#### Scenario: Loopback parent on rootless Podman
- **WHEN** `egress.toml` sets `[egress.upstream] url = "http://127.0.0.1:3128"` and the sidecar starts on rootless Podman with pasta
- **THEN** the sidecar reaches the parent on host loopback, and a sandbox connecting to `host.docker.internal:3128` is refused

#### Scenario: Loopback parent on Docker Engine
- **WHEN** the same configuration is used on Docker Engine
- **THEN** the sidecar reaches the parent through the sidecar-owned parent forwarder, and a sidecar-mode sandbox has no firewall rule for the forwarder's port

#### Scenario: Corporate interception of a spliced host
- **WHEN** the parent intercepts TLS for an allowed, spliced host
- **THEN** the sandbox client validates the parent-minted certificate through the `ca_cert` trust installed at container start

#### Scenario: Denial attribution
- **WHEN** a request is denied by the parent proxy rather than local policy
- **THEN** `project-sandbox-egress log --denied` attributes the denial to the upstream proxy

### Requirement: Sidecar mode is verifiable end to end
The repository SHALL provide an opt-in `egress-sidecar` e2e suite. It SHALL cover direct-egress denial, unlisted host, SNI mismatch, raw tunnel, metadata, allowed splice, GitHub repository access denied, parent-proxy chaining with an intercepting fixture proxy, and a local service listening only on host loopback. It SHALL also cover two concurrent sandboxes on one sidecar, reuse across launches, a live `allow add`, and `down` while a sandbox runs. The suite SHALL run on Apple `container`, Docker Desktop, Podman machine, Docker Engine and rootless Podman with pasta; on the Linux runtimes it SHALL also cover a parent proxy listening only on host loopback. Unit tests SHALL NOT require a container runtime.

#### Scenario: Suite is opt-in
- **WHEN** `scripts/run-e2e-tests.sh` runs without selecting the suite
- **THEN** the `egress-sidecar` suite is skipped

#### Scenario: Shared sidecar coverage
- **WHEN** the `egress-sidecar` suite runs
- **THEN** it starts two sandboxes concurrently, verifies that both use one sidecar, that an `allow add` reaches both without a restart, and that `down` makes both lose egress

#### Scenario: Linux run
- **WHEN** the `egress-sidecar` suite runs on a Linux host with Docker Engine or rootless Podman
- **THEN** it reports the policy probes, the shared-sidecar scenarios, the loopback-only local-service probe and the loopback parent probe for that runtime

## ADDED Requirements

### Requirement: The Docker Engine parent forwarder accepts only the sidecar
On Docker Engine with a parent proxy on host loopback, the sidecar-owned parent forwarder SHALL listen only on the default bridge's gateway address, SHALL close every connection whose peer address is not the sidecar's, and SHALL connect only to host loopback on the parent's port. It SHALL start with the sidecar, stop with `project-sandbox-egress down`, and be restarted by a sidecar-mode launch when it is no longer running; `status` SHALL report it.

#### Scenario: Wrong peer
- **WHEN** a container other than the sidecar connects to the parent forwarder
- **THEN** the forwarder closes the connection without connecting to the parent

#### Scenario: Forwarder died
- **WHEN** the forwarder process was killed and a sidecar-mode sandbox is launched on Docker Engine
- **THEN** the launch restarts it before starting the sandbox, and the sidecar reaches the parent again without a restart
