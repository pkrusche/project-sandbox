# Spec Delta

## MODIFIED Requirements

### Requirement: Sidecar egress is an explicit opt-in mode
The CLI SHALL accept `--egress firewall|sidecar`, defaulting to `firewall`. With `firewall`, egress behaviour SHALL be unchanged. The CLI SHALL reject `--egress sidecar` before any network, key generation, or container work when combined with `--no-firewall`, `--runtime chroot`, `--extra-domain`, `--internet-proxy`, `--ca-cert`, `--allow-github` or `--copilot-host`. Each rejection SHALL name the `egress.toml` key, `project-sandbox-egress` command or egress mode to use instead. Sidecar mode SHALL be supported on Apple `container` (macOS 26 or later with the minimum supported `container` release, see `sidecar-network-isolation`), Colima, Docker Desktop, Podman machine, Docker Engine and rootless Podman on a Linux host; on rootless Docker it SHALL be rejected before any container work, naming the supported runtimes. Devcontainers remain firewall-only: a sidecar-mode launch SHALL NOT render or modify `.devcontainer/` and SHALL say so.

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
- **THEN** the CLI plans the sandbox behind that runtime's global sidecar, using the runtime's published endpoint, instead of rejecting the runtime

#### Scenario: Rootless Docker
- **WHEN** `--egress sidecar` is used with a rootless Docker daemon
- **THEN** the CLI exits with an error naming the supported runtimes, and performs no container work

#### Scenario: Apple container below macOS 26
- **WHEN** `--egress sidecar` is used with Apple `container` on macOS 15
- **THEN** the CLI exits before any network, key, or container work, naming macOS 26 as required

#### Scenario: Per-launch policy flags are rejected
- **WHEN** `--egress sidecar` is combined with `--extra-domain pypi.org`, `--internet-proxy URL` or `--ca-cert PATH`
- **THEN** the CLI exits with an error naming `project-sandbox-egress allow add` or the `egress.toml` upstream keys, and performs no network, key, or container work

#### Scenario: Devcontainer is not rendered in sidecar mode
- **WHEN** a sidecar-mode launch renders sandbox assets
- **THEN** `.devcontainer/` is neither created nor modified, and the CLI prints that devcontainers support firewall egress only

### Requirement: The sidecar's proxy endpoint is published on the host
The global sidecar SHALL publish its proxy port on the port set by `[egress.sidecar] port` in `egress.toml` (default 3129, range 1024–65535), and on exactly one host address: the gateway address of Docker's default bridge on Docker Engine, and the host's loopback address on Docker Desktop, Podman machine and rootless Podman. Colima and Apple `container` are the exceptions and publish no port (see `sidecar-network-isolation`). Starting the sidecar SHALL fail before the container is created when that address and port are not free, naming the key. A sidecar-mode launch SHALL fail before the sandbox starts when the running sidecar publishes a different address or port than the configuration and runtime require, naming `project-sandbox-egress restart`.

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

#### Scenario: Colima publishes nothing
- **WHEN** the sidecar starts on the Colima runtime
- **THEN** no host or VM port is published, and no host port availability check is made

#### Scenario: Apple container publishes nothing
- **WHEN** the sidecar starts on Apple `container`
- **THEN** no host port is published, and no host port availability check is made

### Requirement: The sandbox reaches the Internet only through the sidecar
On the published-endpoint strategy, a sidecar-mode sandbox SHALL be run like a firewall-mode sandbox with an Internet proxy whose endpoint is the sidecar's published endpoint. Its firewall SHALL default-deny, SHALL admit new TCP connections only to the sidecar endpoint and to each local service or remote agent proxy this session uses, and SHALL admit no DNS. It SHALL fail closed when the required IPv6 policy cannot be enforced. Proxy environment variables SHALL point at the sidecar, and `NO_PROXY`/`no_proxy` SHALL keep the local-service bypass entries used in firewall mode. On the isolated strategy, containment is defined by `sidecar-network-isolation` instead.

#### Scenario: Direct egress is blocked
- **WHEN** a process in the sandbox connects directly to a public address or resolves a name through DNS, ignoring proxy variables
- **THEN** the connection or lookup fails

#### Scenario: Host services are not reachable
- **WHEN** a process in the sandbox connects to a host address on a port that is neither the sidecar's nor an enabled local service's
- **THEN** the sandbox firewall drops the connection

#### Scenario: Proxy environment targets the sidecar
- **WHEN** a sidecar-mode session starts with the default port on Docker Desktop
- **THEN** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` are `http://host.docker.internal:3129`, and `NO_PROXY`/`no_proxy` keep the local-service bypass entries used in firewall mode

#### Scenario: Isolated strategy proxy environment
- **WHEN** a sidecar-mode session starts on Apple `container` or Colima
- **THEN** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` are `http://egress:3128`

#### Scenario: Two sandboxes share the sidecar
- **WHEN** two sidecar-mode sandboxes run at the same time on one runtime
- **THEN** both reach the Internet through the same sidecar, and neither launch fails because the other holds a host port

### Requirement: Local services reach sidecar-mode sandboxes as in firewall mode
On the published-endpoint strategy, the agent proxy and Ollama SHALL be forwarded into the sandbox with the same per-runtime local-service strategy as in firewall mode, and the sidecar SHALL NOT forward them. A remote `--agent-proxy` SHALL be admitted as in firewall mode with an Internet proxy: as a direct, port-scoped firewall destination. On the isolated strategy, host-loopback services reach the sandbox only through the sidecar, as defined by `sidecar-local-services`.

#### Scenario: Loopback-only Ollama
- **WHEN** a sidecar-mode session with Ollama enabled starts on Docker Desktop and Ollama listens only on the host's `127.0.0.1:11434`
- **THEN** the sandbox reaches it at `host.docker.internal:11434` without going through the sidecar

#### Scenario: Remote agent proxy
- **WHEN** a sidecar-mode session starts on Docker Desktop with `--agent-proxy https://gateway.corp.example:8443`
- **THEN** the sandbox firewall admits that gateway's resolved addresses on port 8443, and the agent's model traffic does not pass through the sidecar

#### Scenario: Isolated strategy uses the sidecar
- **WHEN** a sidecar-mode session with Ollama enabled starts on Apple `container`
- **THEN** no host alias is mapped into the sandbox, and it reaches Ollama at `egress:11434` through the sidecar

### Requirement: The Colima sandbox admits only the sidecar and uses no DNS
In Colima sidecar mode, the sandbox SHALL admit no DNS: it SHALL resolve `egress` from an entry pinned at launch, not from a DNS server. A launch SHALL fail before the sandbox starts when the sidecar's address on the internal network can't be determined. The sandbox firewall is governed by `sidecar-network-isolation`. When installed, it SHALL default-deny and admit new TCP connections only to the sidecar's internal address on port 3128 and the declared service ports.

#### Scenario: Pinned proxy name
- **WHEN** a Colima sidecar-mode sandbox starts
- **THEN** `egress` resolves to the sidecar's address on the internal network from the sandbox's hosts file

#### Scenario: Opt-in firewall admits only the sidecar
- **WHEN** a Colima sidecar-mode sandbox starts with `--sandbox-firewall`
- **THEN** the firewall allows only the sidecar's internal address on port 3128 and the declared service ports

#### Scenario: Sidecar address is unavailable
- **WHEN** the sidecar is not attached to the internal network at launch
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress restart`
