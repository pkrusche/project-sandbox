# Spec Delta

## MODIFIED Requirements

### Requirement: The sandbox reaches the Internet only through the sidecar
On every runtime except Colima, a sidecar-mode sandbox SHALL be run like a firewall-mode sandbox with an Internet proxy whose endpoint is the sidecar's published endpoint. Its firewall SHALL default-deny, SHALL admit new TCP connections only to the sidecar endpoint and to each local service or remote agent proxy this session uses, and SHALL admit no DNS. It SHALL fail closed when the required IPv6 policy cannot be enforced. Proxy environment variables SHALL point at the sidecar, and `NO_PROXY`/`no_proxy` SHALL keep the local-service bypass entries used in firewall mode. On Colima, containment is defined by "A Colima sandbox has no route out except the sidecar".

#### Scenario: Direct egress is blocked
- **WHEN** a process in the sandbox connects directly to a public address or resolves a name through DNS, ignoring proxy variables
- **THEN** the connection or lookup fails

#### Scenario: Host services are not reachable
- **WHEN** a process in the sandbox connects to a host address on a port that is neither the sidecar's nor an enabled local service's
- **THEN** the sandbox firewall drops the connection

#### Scenario: Proxy environment targets the sidecar
- **WHEN** a sidecar-mode session starts with the default port on Docker Desktop
- **THEN** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` are `http://host.docker.internal:3129`, and `NO_PROXY`/`no_proxy` keep the local-service bypass entries used in firewall mode

#### Scenario: Two sandboxes share the sidecar
- **WHEN** two sidecar-mode sandboxes run at the same time on one runtime
- **THEN** both reach the Internet through the same sidecar, and neither launch fails because the other holds a host port

### Requirement: Local services reach sidecar-mode sandboxes as in firewall mode
On every runtime except Colima, the agent proxy and Ollama SHALL be forwarded into the sandbox with the same per-runtime local-service strategy as in firewall mode, and the sidecar SHALL NOT forward them. A remote `--agent-proxy` SHALL be admitted as in firewall mode with an Internet proxy: as a direct, port-scoped firewall destination. On Colima, host-loopback services reach the sandbox only through the sidecar, as defined by `sidecar-local-services`.

#### Scenario: Loopback-only Ollama
- **WHEN** a sidecar-mode session with Ollama enabled starts on Docker Desktop and Ollama listens only on the host's `127.0.0.1:11434`
- **THEN** the sandbox reaches it at `host.docker.internal:11434` without going through the sidecar

#### Scenario: Remote agent proxy
- **WHEN** a sidecar-mode session starts on Docker Desktop with `--agent-proxy https://gateway.corp.example:8443`
- **THEN** the sandbox firewall admits that gateway's resolved addresses on port 8443, and the agent's model traffic does not pass through the sidecar

#### Scenario: Colima uses the sidecar
- **WHEN** a sidecar-mode session with Ollama enabled starts on Colima and `ollama` is declared on port 11434
- **THEN** no host alias is mapped into the sandbox, and it reaches Ollama at `egress:11434` through the sidecar

### Requirement: Destinations are filtered by hostname with SNI verification
The sidecar SHALL deny every request unless its destination hostname matches the effective allowlist (exact or `*.`-wildcard). It SHALL deny CONNECT to ports other than 443, IP-literal destinations, and (when no parent proxy is set) cloud-metadata, private, loopback and link-local resolved addresses, and resolved addresses in `203.0.113.0/24`, the range of Apple `container`'s localhost alias. All of these denials SHALL be issued before any upstream connection is made, and the sidecar SHALL record the denial cause in its access log. Port, IP-literal and allowlist denials SHALL close the client connection without responding `200 Connection established`. A resolved-address denial (which needs a DNS lookup) MAY answer `200` before the client's TLS handshake fails, but SHALL NOT open the upstream connection. For allowed CONNECTs, the sidecar SHALL peek at the TLS ClientHello and SHALL terminate the connection when the SNI is missing, is not allowlisted, or differs from the CONNECT host, or when the tunnel does not carry TLS.

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

#### Scenario: Localhost alias range
- **WHEN** an allowlisted name resolves to `203.0.113.113` in direct mode on Apple `container`, where the administrator-managed alias redirects that address to the Mac's loopback
- **THEN** the sidecar denies the request without opening an upstream connection

### Requirement: On Colima the sidecar is reached over a private network
On the Colima runtime, the sidecar SHALL be attached to two labelled, project-owned networks inside the profile's VM: an outbound bridge used only by the sidecar, and an `--internal` bridge carrying the alias `egress`. The internal bridge SHALL be created with `com.docker.network.bridge.gateway_mode_ipv4=isolated`, so the VM has no address on it. Network validation SHALL require that option. Sidecar-mode sandboxes SHALL be attached to the internal bridge only and reach the proxy at `egress` on port 3128. Networks are created on demand and reused; a session's end SHALL NOT remove them. An internal bridge without the option SHALL be replaced by `up`, `restart` or a sidecar-mode launch when no container is attached to it. Otherwise these SHALL fail before any container work, naming the attached sandboxes and `project-sandbox-egress restart`.

#### Scenario: Sandbox reaches the proxy privately
- **WHEN** a sidecar-mode sandbox starts on Colima
- **THEN** it is attached to the internal network only, `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy` and `https_proxy` are `http://egress:3128`, and an allowed request succeeds through the sidecar

#### Scenario: Concurrent sandboxes
- **WHEN** two sidecar-mode sandboxes run at the same time on Colima
- **THEN** both reach the Internet through the same sidecar, and neither launch creates or removes a network

#### Scenario: Networks are owned
- **WHEN** a network named like a project network exists without the project's ownership label
- **THEN** the CLI refuses to use or remove it and names the network

#### Scenario: The VM has no address on the internal network
- **WHEN** the internal network is created
- **THEN** it carries `com.docker.network.bridge.gateway_mode_ipv4=isolated`, and the VM's bridge interface has no IPv4 address

#### Scenario: Legacy network is replaced
- **WHEN** `psbx-sandbox` exists without the gateway-mode option and no container is attached, and `project-sandbox-egress up` runs
- **THEN** the network is removed and created again with the option before the sidecar is attached

#### Scenario: Legacy network in use
- **WHEN** `psbx-sandbox` lacks the option and a sandbox is still attached, and a sidecar-mode launch starts
- **THEN** the launch fails before any container work, naming the attached sandbox and `project-sandbox-egress restart`

#### Scenario: Engine without the gateway mode
- **WHEN** the profile's Docker Engine rejects `com.docker.network.bridge.gateway_mode_ipv4`
- **THEN** network creation fails before the sidecar starts, naming the option, Docker Engine 28 as the minimum and the Colima upgrade

### Requirement: A Colima sandbox has no route out except the sidecar
A Colima sidecar-mode sandbox SHALL NOT be attached to the outbound bridge, and SHALL have no route to the Internet, the Mac (`host.lima.internal`), the VM itself, the VM's bridge gateways, the metadata address or the sidecar's outbound address. This SHALL hold with the sandbox firewall off and the sandbox holding no network capabilities. The sidecar SHALL NOT forward IP traffic between its networks.

#### Scenario: Direct access fails regardless of proxy variables
- **WHEN** a process in a Colima sidecar-mode sandbox without the sandbox firewall connects by TCP or UDP to a public IPv4 or IPv6 address, or resolves a public name through any resolver
- **THEN** the connection or lookup fails

#### Scenario: The Mac and the VM are unreachable
- **WHEN** a process in the sandbox connects to `192.168.5.2` on any port, or to the VM's sshd or any port another container publishes on the VM's interfaces
- **THEN** the connection is refused or times out

#### Scenario: No other network path
- **WHEN** a process in the sandbox connects to the VM's default bridge gateway, the sidecar's address on the outbound bridge or `169.254.169.254`
- **THEN** the connection is refused or times out

#### Scenario: Sidecar does not route
- **WHEN** a sandbox process with root and `NET_ADMIN` inside its container sets the sidecar as its default route
- **THEN** no packet reaches the outbound network, the Mac or the Internet

### Requirement: The Colima sandbox admits only the sidecar and uses no DNS
In Colima sidecar mode, the sandbox SHALL admit no DNS: it SHALL resolve `egress` from an entry pinned at launch, not from a DNS server. A launch SHALL fail before the sandbox starts when the sidecar's address on the internal network can't be determined. The sandbox firewall is governed by "The Colima sandbox firewall is opt-in". When installed, it SHALL default-deny and admit new TCP connections only to the sidecar's internal address on port 3128 and the declared service ports.

#### Scenario: Pinned proxy name
- **WHEN** a Colima sidecar-mode sandbox starts
- **THEN** `egress` resolves to the sidecar's address on the internal network from the sandbox's hosts file

#### Scenario: Opt-in firewall admits only the sidecar
- **WHEN** a Colima sidecar-mode sandbox starts with `--sandbox-firewall`
- **THEN** the firewall allows only the sidecar's internal address on port 3128 and the declared service ports

#### Scenario: Sidecar address is unavailable
- **WHEN** the sidecar is not attached to the internal network at launch
- **THEN** the launch fails before the sandbox starts and names `project-sandbox-egress restart`

## ADDED Requirements

### Requirement: The Colima sandbox firewall is opt-in
In Colima sidecar mode, the sandbox SHALL run without network capabilities and without installing its firewall, unless the launch passes `--sandbox-firewall` or the firewall default is on ("The Colima firewall default follows verified containment"). When installed, the firewall follows "The Colima sandbox admits only the sidecar and uses no DNS". Changing the declared services SHALL NOT require an image rebuild.

#### Scenario: Default launch on a verified runtime
- **WHEN** a Colima sidecar-mode sandbox starts without either flag and Colima's containment is verified
- **THEN** the run argv adds no `NET_ADMIN` or `NET_RAW`, and no firewall rules are installed

#### Scenario: Opt-in firewall
- **WHEN** the launch passes `--sandbox-firewall`
- **THEN** the firewall is installed and admits only the sidecar's internal address on port 3128 and the declared service ports

#### Scenario: Service added after the image was built
- **WHEN** a service is declared after the image was built and an opt-in firewall launch starts with `--no-build`
- **THEN** the firewall admits the new service port

### Requirement: The sandbox firewall flags apply only to Colima sidecar mode
`--sandbox-firewall` and `--no-sandbox-firewall` SHALL be mutually exclusive, and SHALL be accepted only in Colima sidecar mode. Otherwise the CLI SHALL exit before any container work and explain where the flags apply.

#### Scenario: Flag on another runtime
- **WHEN** `--sandbox-firewall` or `--no-sandbox-firewall` is used on any runtime other than Colima, or outside sidecar mode
- **THEN** the CLI exits before any container work, explaining that the flag applies only to Colima sidecar mode

#### Scenario: Both flags
- **WHEN** a launch passes both `--sandbox-firewall` and `--no-sandbox-firewall`
- **THEN** the CLI exits before any container work, naming the conflict

### Requirement: The one-shot privilege is revoked when the firewall is skipped
When the sandbox firewall isn't installed, the sandbox SHALL still revoke its one-shot firewall privilege before the session command runs. The session command SHALL NOT be able to run the firewall helper or any other privileged helper afterwards. The decision to skip the firewall SHALL come from a root-owned session input written by the host, not from the environment or arguments.

#### Scenario: Helper unavailable to the agent
- **WHEN** a Colima sandbox without the firewall has started and the session user runs the firewall helper through `sudo -n`
- **THEN** the command is refused

#### Scenario: Environment cannot select the mode
- **WHEN** the session environment sets a variable naming the firewall mode
- **THEN** the helper still follows the session input

### Requirement: The Colima firewall default follows verified containment
Colima's sandbox firewall SHALL default to off only after its containment has been verified: the internal network without a VM address has shipped, and the end-to-end containment checks pass. Until then it SHALL default to on. `--sandbox-firewall` SHALL always install it, and `--no-sandbox-firewall` SHALL turn it off only where containment is verified.

#### Scenario: Unverified runtime
- **WHEN** Colima's containment is not yet verified and a sidecar-mode launch starts
- **THEN** the sandbox firewall is installed, and the launch says why

#### Scenario: Turning it off where unverified
- **WHEN** `--no-sandbox-firewall` is used while Colima's containment is not verified
- **THEN** the CLI exits before any container work and names the reason

### Requirement: Sidecar containment is documented per runtime
The documentation SHALL include a containment matrix for sidecar mode. For each supported runtime it SHALL show whether containment is enforced by the network or by the sandbox firewall, whether the sandbox firewall is on by default, and whether sandboxes can reach one another. It SHALL state why Apple `container` stays firewall-level, and that Colima sandboxes on the internal network can reach one another and every declared service.

#### Scenario: Matrix present
- **WHEN** a user reads the egress sidecar documentation
- **THEN** it lists Apple `container`, Colima, Docker Desktop, Podman machine, Docker Engine and rootless Podman with those three properties

#### Scenario: Lateral reachability is stated
- **WHEN** a user reads the security documentation for Colima sidecar mode
- **THEN** it states that sandboxes on the internal network can reach one another and every declared service
