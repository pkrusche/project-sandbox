## Context

See `proposal.md` for motivation and the capability deltas for observable requirements. Today `ollama_network.py` already contains a mostly generic `ForwardingPlan` and runtime strategy selection, but its module, constants, diagnostics, and some errors remain Ollama-specific. Agentgateway reuses that implementation from `cli.py`. Firewall rendering separately models Ollama and Agentgateway and normally pre-resolves public domains into IPv4/IPv6 ipsets. Session environment is assembled across container, chroot, interactive, and headless launch paths, so proxy variables must have one canonical source to avoid drift.

The filtering proxy is an existing host-loopback service owned by another repository. The design must never treat proxy routing hints as enforcement, must avoid restoring DNS or broad host-gateway access inside the sandbox, and must preserve every non-Internet-proxy path byte-for-byte where practical.

## Goals / Non-Goals

**Goals:**

- Model all loopback services with neutral metadata and one runtime forwarding lifecycle.
- Make Internet-proxy validation, environment generation, preflight, and firewall policy explicit and independently testable.
- Preserve one shared runtime hostname with distinct port-scoped paths for Internet proxy, Agentgateway, and Ollama.
- Make proxy-mode firewall rendering structurally incapable of populating public destination allowlists.
- Keep dry-run side-effect-free while showing enough sanitized state to audit routing.

**Non-Goals:**

- Managing or identifying the external proxy implementation.
- Transparent TCP redirection, TLS interception, CA installation, sidecar networking, or Docker Compose.
- Synchronizing project-sandbox domain flags with external proxy configuration.
- Sending Agentgateway or Ollama traffic through the Internet proxy.

## Decisions

### 1. Replace the Ollama-specific network module with a service descriptor and forwarding plan

Rename `ollama_network.py` to `local_service_network.py` and represent a request with an immutable descriptor containing at least label, host loopback port, and protocol. Keep the existing `ForwardingPlan` lifecycle and runtime adapters, but make all validation and errors use descriptor metadata. All services use the single internal hostname `host.docker.internal`; service identity and authorization remain distinguished by port and, where applicable, URL path.

The neutral plan maps that hostname once per sandbox: to shared loopback for chroot local services outside Internet-proxy mode, to the administrator-managed Apple `container` localhost DNS entry, to `host-gateway` for Docker Desktop and compatible Podman modes, or to the verified bridge address for local Linux bridge runtimes. Managed `socat` listeners on Linux remain separate by port while sharing the bridge address and hostname. Firewall policy retains a separate hostname/port entry for every enabled service. If two services request the same host port, planning must reject the collision rather than silently route both to the wrong upstream.

Alternative considered: add a third set of Internet-proxy branches around `ollama_network`. Rejected because it compounds naming leakage and risks divergence among lifecycle and safety checks.

### 2. Isolate URL parsing and proxy policy in a dedicated Internet proxy module

Add an `internet_proxy` module responsible for strict URL parsing, forwarded URL construction using `host.docker.internal`, canonical proxy/no-proxy environment generation, and bounded listener preflight. URL parsing returns a typed configuration containing the original loopback host, explicit port, and forwarded URL. Use a TCP connect preflight because it proves listener availability without needing an implementation-specific endpoint, a public destination, proxy authentication semantics, or redirect handling.

Run semantic CLI conflicts before secret lookup, runtime inspection, filesystem rendering, or network preflight. Reject chroot because it shares the host network namespace: installing the proxy-only rules there would modify the host firewall, while omitting them would make proxy routing bypassable. Run preflight against the original host-loopback address only on real runs and before starting owned forwarding resources or the sandbox. A failed connection names the configured listener and tells the user to start or troubleshoot the configured proxy; it never attempts repair.

Alternative considered: issue an HTTP request through the proxy. Rejected because a meaningful request either depends on proxy-specific health behavior or on a permitted external destination.

### 3. Treat forwarded services as a collection throughout planning

Build a single ordered collection of enabled local-service descriptors/plans in `cli.py`, with Internet proxy, Agentgateway, and Ollama entries as applicable. Derive one deduplicated `host.docker.internal` mapping for runtime argv construction and feed each service's distinct hostname/port pair to firewall rendering. Preserve service-specific higher-level behavior—Agentgateway model discovery and credentials, and Pi's Ollama provider configuration—outside the forwarding module.

`host.docker.internal` appears once in `NO_PROXY` whenever a local service must bypass general proxy routing. This lets Agentgateway and Ollama requests connect directly to their own ports. It does not bypass general Internet routing: proxy clients compare `NO_PROXY` with the requested destination, not with the configured proxy server's hostname. Loopback literals are always included. The same generated environment list is passed to all supported container, interactive, and headless execution builders instead of being reconstructed in templates.

Alternative considered: export variables only from shell initialization. Rejected because non-shell entrypoints and headless agent processes would not reliably inherit them.

### 4. Add an explicit firewall policy mode and typed local destinations

Extend firewall rendering with a policy mode (normal versus Internet-proxy) and a list of local TCP destinations rather than adding more service-specific scalar arguments. Each destination supplies a label, the shared hostname, and its distinct port and renders an exact destination-address/TCP-port ACCEPT rule. Resolve the shared hostname once and reuse the pinned address for all enabled local-service rules. Existing Ollama and Agentgateway arguments can be migrated internally in the same change while regression tests lock their externally visible behavior.

In Internet-proxy mode, templates do not populate ordinary IPv4/IPv6 ipsets at all and do not render GitHub, provider, extra-domain, resolver, or broad devcontainer host-network exceptions. They retain loopback, connection tracking, required narrow ICMPv6 control traffic, blocked outbound DNS, and terminal DROP policies. The shared local hostname is resolved or pinned by the runtime forwarding plan before the firewall closes DNS. Both iptables and ip6tables setup must succeed; inability to enforce IPv6 aborts rather than degrading to IPv4-only protection.

Alternative considered: pass an empty domain list through the existing normal policy. Rejected because implicit provider/GitHub/devcontainer paths could be reintroduced by future defaults; a distinct mode makes the security invariant reviewable and testable.

### 5. Preserve normal mode and reject incompatible ownership models

CLI validation rejects Internet proxy with `--no-firewall`, `--extra-domain`, or `--allow-github`. No values are silently ignored. Automatic GitHub inference must also be disabled or rejected consistently when Internet proxy mode is selected; user-visible conflicts refer to explicit flags, while internally inferred direct-domain policy must never enter proxy mode.

When Internet proxy mode is absent, use the existing normal firewall inputs and environment path unchanged. This separation minimizes regression risk and supports golden comparisons of generated firewall scripts.

### 6. Document and test three independent failure domains

Unit and renderer tests cover parsing, conflicts, environment generation, runtime plan selection, firewall shape, preflight ordering, and dry-run non-mutation. Existing Ollama and Agentgateway suites remain regression gates after the module rename. End-to-end checks are documented for execution with the separately managed proxy test setup rather than made mandatory unit tests, because CI must not depend on Pipelock, Smokescreen, Apple `container`, or a public destination.

The documentation describes iptables as bypass prevention, the external proxy as the owner of Internet policy, and `agentgateway-locally` as AI/MCP and credential isolation. Runtime loss of either external service is not healed; scoped firewall paths ensure the other remains independent.

## Risks / Trade-offs

- [The shared hostname resolves differently across Apple, Docker, Podman, chroot, and Linux bridge modes] → Centralize one mapping per sandbox in the neutral forwarding module and retain adapter-level regression tests for every runtime strategy.
- [A future normal-mode allowlist default could leak into proxy mode] → Use an explicit template policy branch and assert absence of provider, GitHub, extra-domain, resolver, and broad gateway rules.
- [IPv6 tooling or runtime support may be partial] → Treat inability to install the intended IPv6 policy as fatal in Internet-proxy mode and add failure tests.
- [Some software ignores HTTP proxy variables] → Document that it will fail; firewall denial is the intended secure behavior, not transparent compatibility.
- [A TCP preflight can succeed for a listener that is unhealthy at the application layer] → Keep the check deliberately implementation-neutral and leave health/policy testing to the external repository.
- [Proxy variables may expose a locally meaningful endpoint in dry-run output] → Show only the non-secret forwarded URL and stable bypass list; URLs containing credentials are prohibited.
- [Generalizing an established module can introduce regressions] → Perform the rename before feature wiring, retain compatible behavior at call sites, and run the complete suite plus focused golden tests.

## Migration Plan

1. Introduce the neutral forwarding model, migrate Ollama and Agentgateway to the shared `host.docker.internal` hostname, and retain their existing CLI and port-scoped policy behavior.
2. Add Internet proxy parsing, conflicts, environment generation, and side-effect-free dry-run preview.
3. Add typed firewall destinations and the explicit proxy-only policy, then wire forwarding and preflight.
4. Add documentation and automated coverage, followed by manual acceptance checks against an external filtering proxy on Docker and Apple `container`.

Rollback is removal of the opt-in flag and proxy-only branch; existing sessions and configuration require no data migration. Because the feature is opt-in, reverting leaves non-proxy behavior intact and does not modify the external proxy installation.
