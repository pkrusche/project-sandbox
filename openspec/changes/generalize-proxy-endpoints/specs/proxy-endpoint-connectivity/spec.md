## Purpose

Provide consistent validation, routing, firewall access, and verified TLS for independently configured local and remote proxy endpoints.

## ADDED Requirements

### Requirement: Endpoint URLs distinguish loopback forwarding from remote access
Both proxy options SHALL accept HTTP loopback endpoints and remote HTTP/HTTPS endpoints. HTTPS loopback endpoints SHALL be rejected. Missing ports SHALL default to 80 for HTTP and 443 for HTTPS. Remote DNS names and unicast IPv4/IPv6 literals SHALL be supported without network-provider-specific configuration.

#### Scenario: Local HTTPS is rejected early
- **WHEN** either proxy option specifies HTTPS on localhost or a numeric loopback address
- **THEN** validation rejects it before secret lookup, network access, or generated-file writes

#### Scenario: Corporate HTTPS endpoint uses the default port
- **WHEN** a proxy URL is `https://proxy.corp.example`
- **THEN** its effective port is 443 and the sandbox retains `proxy.corp.example` as the TLS identity

#### Scenario: Remote IPv6 literal is supported
- **WHEN** a proxy uses a valid bracketed non-loopback IPv6 literal with a valid port
- **THEN** it is treated as a direct endpoint and its URL remains correctly bracketed

#### Scenario: Classification cannot be bypassed by spelling
- **WHEN** localhost differs in case or has a trailing dot, or a numeric address represents loopback including IPv4-mapped IPv6
- **THEN** it is classified as loopback and cannot enable local HTTPS through alternate spelling

### Requirement: Endpoint authorities are unambiguous and credential-free
Proxy URLs SHALL reject userinfo including empty userinfo, unsupported schemes, missing hosts, invalid ports, queries, fragments, wildcard/unspecified or multicast addresses, scoped IPv6, and malformed hostnames. Agent URLs SHALL preserve API paths. Internet URLs SHALL allow only an empty path or `/`.

#### Scenario: Embedded Internet proxy credentials are rejected
- **WHEN** an Internet proxy URL contains a username or password
- **THEN** validation rejects it without echoing the credentials and does not attempt proxy authentication

#### Scenario: Invalid endpoint cannot reach privileged rendering
- **WHEN** either endpoint contains shell metacharacters in its hostname, wildcard address, or invalid port
- **THEN** validation fails before firewall generation or network access

### Requirement: Remote endpoints do not require host-loopback resources
Remote proxy connections SHALL originate directly from the sandbox using the configured scheme, hostname, port, and permitted path. Only local services SHALL require runtime forwarding or host-loopback alias setup. Remote services on different hosts SHALL be allowed to use the same port.

#### Scenario: Two remote services share port 443
- **WHEN** both proxies use HTTPS on distinct remote hosts and port 443
- **THEN** both endpoints are accepted without duplicate local-port errors or creation of forwarding listeners

#### Scenario: Mixed local and remote services
- **WHEN** an HTTP loopback gateway is combined with a remote HTTPS Internet proxy
- **THEN** only the gateway uses local forwarding and the Internet proxy retains its remote hostname

### Requirement: Firewall permissions match session-pinned endpoint addresses
With firewall enforcement enabled, each endpoint SHALL be resolved from the sandbox before DNS closes and its admitted addresses pinned for the session. New connections SHALL be permitted only to that endpoint's exact TCP address/port pairs. General DNS SHALL remain blocked. Remote names resolving to loopback, unspecified, or multicast addresses SHALL fail setup.

#### Scenario: Multiple endpoints have different ports
- **WHEN** the Internet proxy resolves to address A on port 8443 and the gateway to address B on port 443
- **THEN** proxy permissions admit A:8443 and B:443 without adding A:443 or B:8443

#### Scenario: Multiple address families are returned
- **WHEN** a remote hostname resolves to multiple usable IPv4 and IPv6 addresses
- **THEN** all admitted addresses are pinned and have exact port rules, and no family is admitted without enforceable policy

#### Scenario: Remote DNS resolves to loopback
- **WHEN** a remote endpoint name returns any loopback address during setup
- **THEN** startup fails instead of routing it through the sandbox's loopback or silently changing to host forwarding

#### Scenario: DNS changes during a session
- **WHEN** the endpoint's DNS answers change after startup
- **THEN** the existing session keeps its admitted address set and restarting the session is required to pick up changes

#### Scenario: IPv6-only destination is unusable
- **WHEN** an endpoint has only IPv6 addresses and IPv6 access or enforcement is unavailable
- **THEN** setup fails before the agent runs without widening access or falling back to an unrelated endpoint

### Requirement: Remote TLS is verified on the host and in the sandbox
HTTPS connections SHALL verify the certificate chain and configured hostname or IP using system trust plus supplied CA certificates. `--ca-cert` SHALL be accepted with either proxy and rejected with neither. Supplied CAs SHALL apply to host preflight and generated sandbox/devcontainer trust without modifying host trust stores. Verification bypass and client certificates SHALL NOT be offered.

#### Scenario: Private CA authenticates an agent-only endpoint
- **WHEN** an HTTPS agent proxy uses a certificate signed by a CA supplied with `--ca-cert` and no Internet proxy is configured
- **THEN** host model discovery and rebuilt sandbox requests trust that CA and verify the configured endpoint name

#### Scenario: Untrusted or mismatched certificate
- **WHEN** the HTTPS endpoint presents an untrusted, expired, or name-mismatched certificate
- **THEN** the relevant preflight or connection fails without disabling verification or downgrading to HTTP

#### Scenario: Invalid certificate input
- **WHEN** a CA input contains a private key, a non-CA leaf certificate, or malformed PEM
- **THEN** validation rejects it before network or generated-file work

#### Scenario: Trust change requires a rebuild
- **WHEN** CA inputs change while an existing image is selected with `--no-build`
- **THEN** the image retains its previous trust and documentation requires rebuilding to apply new CA inputs

### Requirement: Final reachability is verified before the agent starts
Real runs SHALL perform bounded host preflight and verify the endpoint through the final sandbox firewall before agent execution. HTTP Internet proxies SHALL receive a TCP check and HTTPS Internet proxies a verified TLS handshake without requiring a public destination. Diagnostics SHALL distinguish connection and certificate failures and use local runtime guidance only for forwarded endpoints.

#### Scenario: Host can connect but sandbox cannot
- **WHEN** host preflight succeeds and the sandbox cannot reach the configured remote endpoint
- **THEN** startup aborts before agent execution with endpoint-specific connectivity guidance

#### Scenario: HTTPS Internet proxy needs no test destination
- **WHEN** an HTTPS Internet proxy presents a valid certificate and accepts TLS
- **THEN** listener preflight succeeds without issuing a destination-specific proxy request or managing the proxy

### Requirement: Dry-run describes endpoints without contacting them
Dry-run SHALL preview local/direct routing, effective URLs and ports, firewall destinations, proxy bypass entries, and CA installation while marking DNS, TLS, and model discovery as deferred. It SHALL NOT resolve endpoints, read secrets, write files, invoke pass, or start containers or forwarding resources.

#### Scenario: Remote HTTPS preview
- **WHEN** valid remote HTTPS proxy options are supplied with `--dry-run`
- **THEN** the preview retains the remote hostname and displays the direct route without DNS, TLS, filesystem writes, or secret access
