# Spec Delta

## MODIFIED Requirements

### Requirement: The sidecar's proxy endpoint is published on the host
The global sidecar SHALL publish its proxy port on the port set by `[egress.sidecar] port` in `egress.toml` (default 3129, range 1024–65535), and on exactly one host address: the gateway address of Docker's default bridge on Docker Engine, and the host's loopback address on every other runtime. The Colima runtime is the exception and publishes no port (see "On Colima the sidecar is reached over a private network"). Starting the sidecar SHALL fail before the container is created when that address and port are not free, naming the key. A sidecar-mode launch SHALL fail before the sandbox starts when the running sidecar publishes a different address or port than the configuration and runtime require, naming `project-sandbox-egress restart`.

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

## ADDED Requirements

### Requirement: On Colima the sidecar is reached over a private network
On the Colima runtime, the sidecar SHALL be attached to two labelled, project-owned networks inside the profile's VM: an outbound bridge used only by the sidecar, and an `--internal` bridge carrying the alias `egress`. Sidecar-mode sandboxes SHALL be attached to the internal bridge only, and SHALL reach the proxy at `egress` on the fixed container port 3128. The sandbox SHALL NOT be attached to the outbound bridge or have any route to the Internet except through the sidecar. Networks are created on demand and reused; a session's end SHALL NOT remove them.

#### Scenario: Sandbox reaches the proxy privately
- **WHEN** a sidecar-mode sandbox starts on Colima
- **THEN** it is attached to the internal network only, `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy` and `https_proxy` are `http://egress:3128`, and an allowed request succeeds through the sidecar

#### Scenario: Direct access fails regardless of proxy variables
- **WHEN** a process in a Colima sidecar-mode sandbox connects to a public address, or resolves a public name, ignoring the proxy variables
- **THEN** the connection or lookup fails

#### Scenario: No other network path
- **WHEN** a process in the sandbox connects to the VM's bridge gateway or to the sidecar's address on the outbound bridge
- **THEN** the connection is refused or times out, and the sidecar does not forward arbitrary IP traffic

#### Scenario: Concurrent sandboxes
- **WHEN** two sidecar-mode sandboxes run at the same time on Colima
- **THEN** both reach the Internet through the same sidecar, and neither launch creates or removes a network

#### Scenario: Networks are owned
- **WHEN** a network named like a project network exists without the project's ownership label
- **THEN** the CLI refuses to use or remove it and names the network

### Requirement: The Colima sandbox admits only the sidecar and uses no DNS
In Colima sidecar mode, the sandbox firewall SHALL default-deny and SHALL admit new TCP connections only to the sidecar's address on the internal network at port 3128. It SHALL admit no DNS: the sandbox SHALL resolve `egress` from an entry pinned at launch rather than from a DNS server. A launch SHALL fail before the sandbox starts when the sidecar's address on the internal network cannot be determined.

#### Scenario: Pinned proxy name
- **WHEN** a Colima sidecar-mode sandbox starts
- **THEN** `egress` resolves to the sidecar's address on the internal network from the sandbox's hosts file, and the firewall allows only that address on port 3128

#### Scenario: Sidecar address is unavailable
- **WHEN** the sidecar is not attached to the internal network at launch
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress restart`

### Requirement: The Colima proxy port is fixed
On the Colima runtime the sidecar's proxy port is not configurable. A Colima sidecar-mode launch, and `project-sandbox-egress up`, SHALL fail naming `[egress.sidecar] port` when it is set to a value other than the default. The setting SHALL keep its meaning on every other runtime.

#### Scenario: Non-default port
- **WHEN** `egress.toml` sets `[egress.sidecar] port = 3140` and a sidecar-mode launch uses `--runtime colima`
- **THEN** the launch fails before any container work and names the key

#### Scenario: Other runtimes are unchanged
- **WHEN** the same configuration is used with `--runtime docker`
- **THEN** the sidecar publishes port 3140 as before

### Requirement: Colima sidecar state is scoped by profile
Each Colima profile SHALL have its own sidecar, networks, state, CA and locks, so that a launch on one profile neither reads nor changes another profile's sidecar. `project-sandbox-egress` commands SHALL act on the profile selected by `--runtime colima` and `--colima-profile`.

#### Scenario: Two profiles
- **WHEN** sidecar-mode sandboxes run on profile `a` and profile `b`
- **THEN** each profile has its own sidecar and networks, and `project-sandbox-egress down` on `a` leaves `b` running

#### Scenario: Restart drift
- **WHEN** a Colima sidecar is recreated, for example after a restart-affecting setting changes
- **THEN** the new sidecar is attached to both networks before it is reported ready, and sandboxes already running keep failing closed rather than gaining a route
