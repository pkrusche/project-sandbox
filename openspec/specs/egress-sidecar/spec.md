# egress-sidecar Specification

## Purpose
The opt-in `--egress sidecar` mode routes a sandbox's Internet traffic through a Squid sidecar that filters by hostname and TLS SNI instead of pinned IP addresses. It blocks GitHub repository access, admits Copilot through a narrow profile, and can chain to an upstream parent proxy.

## Requirements

### Requirement: Sidecar egress is an explicit opt-in mode
The CLI SHALL accept `--egress firewall|sidecar`, defaulting to `firewall`. With `firewall`, egress behaviour SHALL be unchanged. The CLI SHALL reject `--egress sidecar` before any network, key generation, or container work when combined with `--no-firewall` or chroot. Devcontainers remain firewall-only: a sidecar-mode launch SHALL NOT render or modify `.devcontainer/` and SHALL say so.

#### Scenario: Default mode is unchanged
- **WHEN** the user omits `--egress`
- **THEN** the rendered firewall, run argv, and environment are identical to those produced before this change

#### Scenario: Sidecar mode is selected
- **WHEN** the user runs with `--egress sidecar --runtime docker`
- **THEN** the CLI plans a per-session internal network, a Squid sidecar, and a sandbox attached only to that internal network

#### Scenario: Unsupported combination
- **WHEN** `--egress sidecar` is combined with `--no-firewall` or `--runtime chroot`
- **THEN** the CLI exits with an error naming the conflict and performs no network, key, or container work

#### Scenario: Devcontainer is not rendered in sidecar mode
- **WHEN** a sidecar-mode launch renders sandbox assets
- **THEN** `.devcontainer/` is neither created nor modified, and the CLI prints that devcontainers support firewall egress only

### Requirement: The sandbox reaches the Internet only through its sidecar
In sidecar mode, the system SHALL create a per-session internal network on Docker, Podman, and Apple `container`. The sandbox SHALL be attached only to that network, and the sidecar SHALL be attached to both that network and the runtime's egress network. The sandbox firewall SHALL default-deny and SHALL admit only TCP to the sidecar's proxy port and to the sidecar on each enabled local-service port. It SHALL admit no DNS and no host address. The sidecar SHALL forward each enabled local-service port to the host-local service, accepting connections only from the session's internal subnet, and the sandbox SHALL resolve `host.docker.internal` to the sidecar. Proxy environment variables SHALL point at the sidecar.

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
- **WHEN** a sidecar-mode session with Ollama or the agent proxy enabled starts on any runtime
- **THEN** the sandbox reaches `host.docker.internal:<port>` through the sidecar's forwarder, and the sandbox firewall admits that port only on the sidecar's internal IP

#### Scenario: Apple container topology
- **WHEN** a sidecar-mode session starts on `--runtime apple-container`
- **THEN** the run argv attaches the sandbox only to a network created with `container network create --internal`

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

### Requirement: TLS interception is minimal and uses a per-session name-constrained CA
The system SHALL intercept TLS at most for `api.github.com`, and only when the Copilot profile is enabled and that host is handled in path-restricted mode. It SHALL splice all other allowed hosts. The system SHALL generate a fresh CA on the host for each sidecar session with `BasicConstraints(ca=True, pathlen=0)` and critical name constraints permitting the intercepted hosts' registrable domain (Squid mimics the origin certificate's names), or only the reserved name `invalid` when nothing is intercepted. The private key SHALL be stored mode 0600, mounted read-only only into the sidecar, and deleted at teardown. When interception is active, the public certificate SHALL be installed into the sandbox's system trust at container start, not in the image. Under `--dry-run`, the system SHALL NOT generate a key.

#### Scenario: Nothing intercepted means no trusted CA
- **WHEN** a sidecar session intercepts no host
- **THEN** the session CA is constrained to `invalid`, nothing is mounted into the sandbox, and every allowed host is spliced

#### Scenario: CA is constrained
- **WHEN** `api.github.com` is intercepted for the Copilot token exchange
- **THEN** the generated CA certificate carries a critical name constraint permitting only `github.com` and its subdomains

#### Scenario: Key stays out of the sandbox
- **WHEN** a session with interception starts
- **THEN** the sandbox run argv contains a mount for the public certificate only, and the private key is mounted only into the sidecar

#### Scenario: Image cache is unaffected
- **WHEN** two consecutive sessions generate different CAs
- **THEN** the sandbox image checksum is the same for both

#### Scenario: Spliced hosts are untouched
- **WHEN** a client connects to an allowed host other than an intercepted one
- **THEN** the client receives the origin's (or upstream proxy's) certificate, not one minted by the session CA

### Requirement: GitHub repository access is blocked in sidecar mode
The sidecar SHALL NOT admit GitHub repository hosts (`github.com`, `api.github.com`, `uploads.github.com`, `codeload.github.com`, `gist.github.com`, `raw.githubusercontent.com`, `gist.githubusercontent.com`, `media.githubusercontent.com`, `objects.githubusercontent.com`, `github-cloud.githubusercontent.com`, `release-assets.githubusercontent.com`), except `api.github.com` restricted to the Copilot token-exchange paths. The CLI SHALL reject these hosts in the policy file's `allow` and in `--extra-domain`, including through a covering wildcard, and SHALL reject `--allow-github` and an `[egress.github]` policy table in sidecar mode, pointing to firewall mode and the roadmap.

#### Scenario: Clone is denied
- **WHEN** the agent runs `git clone https://github.com/acme/tool` in a sidecar session
- **THEN** the connection is denied and the denial is logged with its cause

#### Scenario: Repository API is denied with Copilot enabled
- **WHEN** the Copilot profile intercepts `api.github.com` and the agent requests `https://api.github.com/repos/acme/tool`
- **THEN** the request is denied with a labelled 403

#### Scenario: Wildcard cannot re-admit GitHub
- **WHEN** the policy file allows `*.githubusercontent.com` or `--extra-domain github.com` is given in sidecar mode
- **THEN** the CLI exits with an error naming the GitHub repository hosts it would admit

#### Scenario: --allow-github in sidecar mode
- **WHEN** `--egress sidecar` is combined with `--allow-github`
- **THEN** the CLI exits with an error before any network, key, or container work

### Requirement: Copilot access is a separate host profile
The policy SHALL offer a Copilot profile, enabled by the policy file or implied by `--agent copilot`, that admits the GitHub Copilot service hosts without granting repository access. The profile SHALL NOT admit Copilot telemetry hosts. The Copilot service hosts SHALL be spliced. If the Copilot CLI needs `api.github.com`, the profile SHALL admit it only through interception with an allowlist of Copilot token-exchange paths. The repository SHALL provide an opt-in, billable `copilot-egress` e2e suite that records the Copilot CLI's hosts and paths and verifies the profile.

#### Scenario: Copilot without repositories
- **WHEN** the Copilot profile is enabled
- **THEN** a Copilot CLI session succeeds and `git clone https://github.com/acme/tool` is denied

#### Scenario: Copilot telemetry is blocked
- **WHEN** the Copilot CLI posts telemetry to `telemetry.individual.githubcopilot.com` in a sidecar session
- **THEN** the connection is denied and the Copilot session still succeeds

#### Scenario: Profile verification
- **WHEN** the `copilot-egress` suite runs
- **THEN** it reports the hosts and paths Copilot used, whether the profile works without `api.github.com`, and that every GitHub repository probe was denied

### Requirement: Policy lives on the host outside the sandbox's reach
The system SHALL read sidecar policy from `--egress-policy PATH`, or else from `$XDG_CONFIG_HOME/project-sandbox/egress.toml` when that file exists. The policy SHALL be merged with `--extra-domain` and agent provider hosts. The system SHALL reject a policy path that resolves inside the project, the worktree, the main repository's `.git`, or any other directory mounted into the sandbox. It SHALL reject unknown keys and invalid hostnames. Rendered sidecar config SHALL be written to the session state directory, not to the project.

#### Scenario: Policy inside the project
- **WHEN** `--egress-policy ./egress.toml` resolves inside the project directory
- **THEN** the CLI exits with an error explaining that the agent could modify it

#### Scenario: Effective policy preview
- **WHEN** the user runs with `--egress sidecar --dry-run`
- **THEN** the output shows the policy path, effective allowlist, Copilot profile, intercepted hosts, and CA constraints, and no file, key, network, or container is created

#### Scenario: Unknown key
- **WHEN** the policy file contains an unrecognized key
- **THEN** the CLI exits with an error naming the key

### Requirement: The sidecar can chain to an upstream parent proxy
In sidecar mode, `--internet-proxy URL` SHALL configure the sidecar to forward all traffic through that proxy as its only parent, never going direct. `--ca-cert` inputs SHALL be trusted by the sidecar for upstream TLS as well as installed in the sandbox image. In parent mode, local policy SHALL be hostname-based and SHALL NOT require local DNS resolution of destinations. On Apple `container`, the sidecar SHALL reach the parent through the host relay, using a per-session port. Upstream proxy authentication is not supported, and HTTP 407 SHALL fail the request.

#### Scenario: Corporate parent on Docker
- **WHEN** the user runs `--egress sidecar --internet-proxy https://proxy.corp.example --ca-cert corp.pem --runtime docker`
- **THEN** the rendered Squid config declares that proxy as the sole parent with TLS verified against system trust plus `corp.pem`, and contains no rule permitting direct forwarding

#### Scenario: Corporate parent on Apple container
- **WHEN** the same options are used with `--runtime apple-container`
- **THEN** the sidecar's parent is the host relay on `host.docker.internal` at a session-specific port, and two concurrent sessions using the same upstream both start

#### Scenario: Corporate interception of a spliced host
- **WHEN** the parent intercepts TLS for an allowed, spliced host
- **THEN** the sandbox client validates the parent-minted certificate through the `--ca-cert` trust in its image

#### Scenario: Denial attribution
- **WHEN** a request is denied by the parent proxy rather than local policy
- **THEN** the session-end summary attributes the denial to the upstream proxy

### Requirement: Sidecar lifecycle is managed and leaves no residue
The system SHALL start the sidecar and wait until it is ready, within a bounded timeout, before starting the sandbox. It SHALL remove the sidecar, the internal network, and the session CA directory when the session ends, including on error. It SHALL remove labelled sidecar resources left behind by sessions that no longer hold a session lock at the next launch and from `project-sandbox doctor`.

#### Scenario: Sidecar fails to become ready
- **WHEN** the sidecar does not answer within the readiness timeout
- **THEN** the sandbox is not started, the CLI reports the sidecar log tail, and all created resources are removed

#### Scenario: Normal exit
- **WHEN** the sandbox exits
- **THEN** the sidecar container, internal network, and CA directory no longer exist

#### Scenario: Orphan from a crash
- **WHEN** a previous session was killed and left a labelled sidecar and network behind
- **THEN** the next launch removes them before creating new resources

### Requirement: Denials are observable
Local denials SHALL be labelled distinctly per cause, as a labelled response page for plain HTTP and intercepted requests and as an access-log cause annotation for CONNECT-level and TLS-level denials: not allowlisted, SNI mismatch, non-TLS tunnel, IP literal, port, private or metadata address, and path not allowed on an intercepted host. At session end, the CLI SHALL print a bounded summary of denied destinations with the policy path to edit.

#### Scenario: Blocked host summary
- **WHEN** the agent was denied `example.org` during a session
- **THEN** the session-end output lists `example.org` with its cause and the effective policy path

### Requirement: Sidecar mode is verifiable end to end
The repository SHALL provide an opt-in `egress-sidecar` e2e suite covering: direct-egress denial, unlisted host, SNI mismatch, raw tunnel, metadata, allowed splice, GitHub repository access denied, a Copilot session through the profile, and parent-proxy chaining with an intercepting fixture proxy. Unit tests SHALL NOT require a container runtime.

#### Scenario: Suite is opt-in
- **WHEN** `scripts/run-e2e-tests.sh` runs without selecting the suite
- **THEN** the `egress-sidecar` suite is skipped
