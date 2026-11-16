# Spec Delta

## Purpose

Makes the container network, not the sandbox's own firewall, the containment boundary for sidecar-mode sandboxes on Colima and Apple `container`. The sidecar is the only peer a sandbox can reach.

## ADDED Requirements

### Requirement: Colima and Apple container use the isolated sidecar strategy
Sidecar mode on Colima and on Apple `container` SHALL use the isolated strategy. Sandboxes are attached only to a project-owned internal network with no route out, and the sidecar is attached to that network as `egress` and to one outbound network. Docker Desktop, Podman machine, Docker Engine and rootless Podman SHALL keep the published-endpoint strategy.

#### Scenario: Apple sandbox placement
- **WHEN** a sidecar-mode sandbox starts on Apple `container`
- **THEN** it is attached only to the project's host-only network, and `egress` resolves to the sidecar's address on that network

#### Scenario: Colima sandbox placement
- **WHEN** a sidecar-mode sandbox starts on Colima
- **THEN** it is attached only to `psbx-sandbox`, as before this change

#### Scenario: Other runtimes are unchanged
- **WHEN** a sidecar-mode sandbox starts on Docker Desktop
- **THEN** its run argv, firewall and environment are identical to those produced before this change

### Requirement: Apple container networks are project-owned and reused
On Apple `container`, the isolated strategy SHALL use a labelled, project-owned host-only network for sandboxes. It is created on first use without administrator rights, reused afterwards, and never removed by a session. The CLI SHALL refuse to use or remove a same-named network without the project's ownership label, and SHALL name it.

#### Scenario: First use creates the network
- **WHEN** no project host-only network exists and a sidecar-mode launch starts on Apple `container`
- **THEN** the network is created without `sudo`, and a second launch reuses it

#### Scenario: Foreign network
- **WHEN** a network with the project's name exists without the ownership label
- **THEN** the launch fails before any container starts, naming the network

### Requirement: An isolated sandbox has no route out except the sidecar
On the isolated strategy, a sandbox SHALL have no route to the Internet, the host, the runtime's gateway addresses, the metadata address or the sidecar's outbound address. This SHALL hold with the sandbox firewall disabled and the sandbox holding no network capabilities. The sidecar SHALL NOT forward IP traffic between its networks.

#### Scenario: Direct connections fail without the sandbox firewall
- **WHEN** a process in an isolated sandbox without `--sandbox-firewall` connects by TCP or UDP to a public IPv4 or IPv6 address, the host's address on the internal network, `192.168.5.2` on Colima, `203.0.113.113` on Apple, or `169.254.169.254`
- **THEN** the connection is refused or times out

#### Scenario: No external DNS
- **WHEN** a process in an isolated sandbox resolves a public name through any resolver
- **THEN** the lookup fails

#### Scenario: Sidecar does not route
- **WHEN** a sandbox process with root inside its container sets the sidecar as its default route
- **THEN** no packet reaches the outbound network

### Requirement: The sandbox reaches the sidecar by a pinned name
An isolated sandbox SHALL resolve `egress` from an entry pinned at launch to the sidecar's current address on the internal network, not from a DNS server. A launch SHALL fail before the sandbox starts when that address can't be determined, naming `project-sandbox-egress restart`.

#### Scenario: Pinned address
- **WHEN** an isolated sandbox starts
- **THEN** `egress` resolves from the sandbox's hosts file to the sidecar's address on the internal network

#### Scenario: Sidecar detached
- **WHEN** the sidecar isn't attached to the internal network at launch
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress restart`

### Requirement: The isolated sidecar publishes nothing on the host
On the isolated strategy, the sidecar SHALL NOT publish a port on the host, and a launch SHALL NOT bind a listener on the host for the sandbox. Readiness SHALL be checked without a published port. `[egress.sidecar] port` SHALL be rejected when set to a non-default value, naming the key.

#### Scenario: Concurrent launches
- **WHEN** two sidecar-mode launches start at the same time on Apple `container`
- **THEN** both succeed, share one sidecar, and neither reports an address in use

#### Scenario: Non-default port on Apple
- **WHEN** `egress.toml` sets `[egress.sidecar] port = 3140` and a sidecar-mode launch uses Apple `container`
- **THEN** the launch fails before any container work and names the key

### Requirement: The sandbox firewall is opt-in on the isolated strategy
On the isolated strategy, the sandbox SHALL run without network capabilities and without installing its firewall, unless the launch passes `--sandbox-firewall`. With the flag, the firewall SHALL default-deny, admit TCP only to the sidecar's internal address on the proxy and declared service ports, and admit no DNS. Changing the declared services SHALL NOT require an image rebuild.

#### Scenario: Default launch
- **WHEN** an isolated sidecar-mode sandbox starts without `--sandbox-firewall` on a runtime whose default is off
- **THEN** the run argv adds no `NET_ADMIN` or `NET_RAW`, and no firewall rules are installed

#### Scenario: Opt-in firewall
- **WHEN** the launch passes `--sandbox-firewall`
- **THEN** the firewall is installed and admits only the sidecar's internal address on port 3128 and the declared service ports

#### Scenario: Service added after the image was built
- **WHEN** a service is declared after the image was built and an opt-in firewall launch starts with `--no-build`
- **THEN** the firewall admits the new service port

#### Scenario: Flag on another runtime
- **WHEN** `--sandbox-firewall` is used on a runtime with the published-endpoint strategy, or outside sidecar mode
- **THEN** the CLI exits before any container work, explaining that the flag applies only to the isolated sidecar strategy

### Requirement: The one-shot privilege is revoked when the firewall is skipped
When the sandbox firewall isn't installed, the sandbox SHALL still revoke its one-shot firewall privilege before the session command runs. The session command SHALL NOT be able to run the firewall helper or any other privileged helper afterwards.

#### Scenario: Helper unavailable to the agent
- **WHEN** an isolated sandbox without `--sandbox-firewall` has started and the session user runs the firewall helper through `sudo -n`
- **THEN** the command is refused

### Requirement: The firewall default follows verified containment
A runtime's sandbox firewall SHALL default to off only after that runtime's containment has been verified by the recorded spike and the end-to-end checks. A runtime without verified containment SHALL default to on. `--sandbox-firewall` SHALL always install it, and `--no-sandbox-firewall` SHALL turn it off only where containment is verified.

#### Scenario: Unverified runtime
- **WHEN** a runtime's isolated strategy has no verified containment and a sidecar-mode launch starts
- **THEN** the sandbox firewall is installed, and the launch says why

#### Scenario: Turning it off where unverified
- **WHEN** `--no-sandbox-firewall` is used on a runtime without verified containment
- **THEN** the CLI exits before any container work and names the reason

### Requirement: Apple container sidecar mode needs macOS 26
Sidecar mode on Apple `container` SHALL require macOS 26 or later and the minimum supported `container` release. Otherwise the launch, `project-sandbox-egress up` and `doctor` SHALL report it, and the first two SHALL fail before any container, network or key work. The error SHALL name the detected and required versions.

#### Scenario: macOS 15
- **WHEN** a sidecar-mode launch on Apple `container` runs on macOS 15
- **THEN** it fails before any container work, naming macOS 26 as required

#### Scenario: Old container release
- **WHEN** the installed `container` is older than the minimum
- **THEN** the launch fails before any container work, naming both versions

#### Scenario: Firewall mode on macOS 15
- **WHEN** a firewall-mode launch on Apple `container` runs on macOS 15
- **THEN** it proceeds as before this change

### Requirement: Containment is documented per runtime
The documentation SHALL include a containment matrix for sidecar mode. For each supported runtime it SHALL show whether containment is enforced by the network or by the sandbox firewall, whether the sandbox firewall is on by default, and whether sandboxes can reach one another.

#### Scenario: Matrix present
- **WHEN** a user reads the egress sidecar documentation
- **THEN** it lists Apple `container`, Colima, Docker Desktop, Podman machine, Docker Engine and rootless Podman with those three properties

#### Scenario: Lateral reachability is stated
- **WHEN** a user reads the security documentation for the isolated strategy
- **THEN** it states that sandboxes on the internal network can reach one another and every declared service
