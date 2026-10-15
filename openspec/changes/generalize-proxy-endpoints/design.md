# Design

## Context

See `proposal.md` for motivation and scope. `internet_proxy.py` and `agent_proxy.py` currently validate only HTTP loopback URLs and rewrite them to `host.docker.internal`. `local_service_network.py` manages runtime-specific forwarding and rejects colliding local ports. The firewall resolves only the first local destination and permits all service ports on that address. Agent discovery runs on the host; final reachability checks run inside the sandbox. `--ca-cert` currently requires Internet proxy mode and installs CA certificates in generated images.

## Goals / Non-Goals

**Goals:** Share endpoint interpretation across CLI validation, dry-run, discovery, environment, provider configuration, and firewall planning. Keep local forwarding separate from remote routing. Preserve exact address/port enforcement and keep TLS names consistent with application URLs.

**Non-Goals:** TLS forwarding or termination, transparent proxying, client certificates, Internet-proxy authentication, VPN management, dynamic DNS refresh, arbitrary client trust-store configuration, or changes to which agents support agent-proxy mode.

## Decisions

### 1. Represent endpoints independently of forwarding

Introduce immutable shared endpoint metadata: scheme, hostname, effective port, API path where permitted, and local/remote classification. Derive the sandbox URL and authorized destination from that metadata. Only loopback services enter `local_service_network.prepare_services`; remote destinations never request host aliases, socat, or Apple localhost setup.

Recognize canonical `localhost` (case-insensitive, including a trailing dot) and numeric loopback addresses; preserve existing `127.0.0.1` and `::1` behavior. Validate IP literals with an address parser and DNS names with strict hostname validation. Reject unspecified/wildcard, multicast, scoped IPv6, malformed authorities, URL userinfo (even empty), queries, and fragments. Reject HTTPS for loopback endpoints before secret/network work. For remote names, reject resolution to loopback, unspecified, or multicast addresses rather than silently switching routing modes. Private unicast addresses are valid corporate endpoints. IPv4-mapped IPv6 must not evade address classification.

Accept omitted ports as 80/443. Preserve remote scheme, hostname identity, explicit/effective port, and agent API path; format bracketed IPv6 correctly. Internet proxy paths remain empty or `/`. Existing loopback HTTP URLs retain forwarding behavior. Duplicate-port rejection applies only to actual local forwarding listeners; unrelated remote services can share a port.

Alternative: widen each existing parser and reuse local forwarding everywhere. Rejected because remote reachability, TLS identity, and firewall rules would remain coupled to a false shared-host assumption.

### 2. Resolve each destination in the sandbox and pin for the session

Generalize firewall inputs to TCP destinations, with a distinct hostname and port for each endpoint. Before closing DNS, resolve each name using sandbox-visible resolution, validate the resulting IPv4/IPv6 addresses, and pin the admitted addresses in `/etc/hosts`. Numeric endpoints need no DNS. Use the same address set for client resolution and firewall authorization. Deduplicate repeated endpoints and host pins; do not form a cross-product of every service address and every service port.

For remote names, pin all usable returned A/AAAA addresses whose firewall family is enforceable, and authorize exact TCP destination pairs. Any prohibited resolution result aborts setup. If no address can be safely admitted, fail before agent execution. IPv6-only endpoints require IPv6 connectivity and enforceable IPv6 rules. In Internet proxy mode, retain the existing requirement that IPv6 denial must be enforceable even for an IPv4 proxy. In agent-only mode, preserve existing IPv6-disable fallback where applicable and omit unusable IPv6 pins.

A session retains its startup address set; DNS changes require restart. General outbound DNS stays blocked. With `--no-firewall` in agent-only mode, retain ordinary resolver behavior and do not promise pinning or egress enforcement; existing local alias warnings apply only to forwarded endpoints.

Alternative: resolve only on the host or continuously refresh DNS. Host-only resolution can disagree with container networking; refresh introduces additional privileged lifecycle and policy machinery. Neither is necessary for this version.

### 3. Keep routing and credentials independent

Internet proxy environment variables use its computed sandbox URL in both cases. Generate identical upper/lowercase proxy and bypass values. Add configured agent endpoint hostnames to `NO_PROXY` and `no_proxy`; add `host.docker.internal` only when a local agent gateway or Ollama needs it. Deduplicate entries, avoid wildcard or whole-network bypasses, and retain existing loopback bypass entries. Firewall permissions remain exact even where a client's bypass matching covers more than one port.

Internet-proxy-only sessions retain normal agent/provider credentials and route proxy-aware requests through the Internet proxy. Agent-proxy sessions retain credential isolation and bearer authentication, including with a remote HTTP endpoint explicitly selected by the user. Discovery connects directly to the configured gateway and does not inherit ambient host proxy routing; redirect following remains disabled to keep the key on the configured origin.

Alternative: route the gateway through the Internet proxy. Rejected because it changes service independence and failure behavior. No new Internet-proxy authentication mechanism or ambient credential forwarding is introduced.

### 4. Apply verified TLS at both execution locations

Remote HTTPS retains the URL hostname for certificate matching and SNI where applicable; literal-IP URLs require a certificate valid for that IP. Use normal public trust plus optional `--ca-cert` inputs. Permit that flag with either proxy, retain rejection when neither is configured, and preserve existing public CA-only PEM validation (no private keys or directly trusted leaf certificates).

Load supplied CAs into the host-side TLS contexts for model discovery and HTTPS Internet-proxy preflight without modifying host trust stores. Reuse the existing generated image/devcontainer trust installation for sandbox requests. Existing `--no-build` behavior remains: users must rebuild images after changing CAs. Do not add insecure TLS flags or certificate-name overrides. TLS to an Internet proxy is independent of TLS to the destination; supplying a CA does not enable interception.

Internet proxy preflight remains bounded and destination-independent: TCP for HTTP, a verified TLS handshake for HTTPS. Perform equivalent checks inside the sandbox after firewall installation and before launching the agent; host reachability alone is insufficient. Agent discovery still validates the authenticated model catalog on the host; its final sandbox reachability probe must honor the configured scheme, authority, and base path and bypass proxy routing. Distinguish DNS, connect, certificate, and gateway-auth failures without leaking keys. Remote failures must not suggest Apple localhost setup.

Alternative: install certificates only in the image. Rejected because host-side discovery would fail for a private CA before that image is used.

### 5. Preserve policy boundaries and verify actual clients

Internet proxy mode still rejects chroot, `--no-firewall`, `--extra-domain`, and `--allow-github`. The firewall permits only configured proxy endpoints and explicit local services, without adding provider, GitHub, broad gateway, or general DNS exceptions. Agent-only mode retains its existing explicit domain options. Ollama remains an HTTP loopback service with existing lifecycle and configuration.

Validate proxy transport with curl and representative bundled agent/client paths, including both HTTP requests and CONNECT for HTTPS destinations. HTTPS forward-proxy support varies by client; document the tested clients and versions and any unsupported path. Applications that ignore or cannot use proxy configuration fail under the firewall; do not add an automatic HTTP downgrade or direct fallback.

## Risks / Trade-offs

- [Host and sandbox routes or DNS differ] → Resolve/pin in the sandbox and validate the final path before starting the agent; require operators to provide connectivity.
- [DNS rotation breaks a long session] → Document session pinning and restart behavior; retain all admitted startup addresses.
- [Firewall authorizes an IP/port, not an HTTP virtual host] → Document that services sharing the same IP and port share the network exception; TLS verifies identity for compliant clients but is not a process-level egress restriction.
- [Client proxy and CA-store behavior differs] → Test shipped clients, report precise limitations, and preserve denial instead of fallback.
- [Additional CAs expand trust across the image] → Keep the existing explicit public-CA installation model and document its image-wide scope.
- [Shared local networking regresses] → Retain mixed local/remote, same-port remote, Ollama, and runtime forwarding regression coverage.

## Migration Plan

1. Implement shared endpoint interpretation, then migrate callers without changing existing HTTP loopback behavior.
2. Generalize destination-specific firewall resolution and bypass generation; wire remote URLs, CA trust, and final probes.
3. Add unit/regression coverage, controlled HTTP/HTTPS integration checks, and documentation under `docs/`. Record deferred Internet-proxy authentication and DNS refresh in `TODO.md` during implementation.
4. Apply deltas against the main specs synced from `2026-10-14-add-internet-proxy-support`. Preserve independent Ollama behavior while limiting shared-alias statements to local services. Update the agent and Internet capability purposes from local-only wording at final spec sync time.
5. Rebuild generated images to apply new CA inputs. Rollback removes remote endpoint support and regenerates assets; existing HTTP loopback commands remain valid and external proxy services are never modified.

No product decisions remain open for this proposal. Client compatibility is a verification task, not permission to weaken TLS or firewall policy.
