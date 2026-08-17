## 1. Generalize Local-Service Forwarding

- [ ] 1.1 Rename `ollama_network.py` to `local_service_network.py` and introduce typed service metadata for label, loopback port, and protocol while retaining the existing forwarding-plan lifecycle.
- [ ] 1.2 Make chroot, Apple `container`, Docker Desktop, Podman, and Linux bridge/managed-`socat` strategies service-neutral and map the single internal hostname `host.docker.internal` once per sandbox.
- [ ] 1.3 Support the shared hostname through Apple administrator-managed DNS, Docker Desktop and compatible Podman `host-gateway`, Linux bridge address pinning with per-port `socat` listeners, and chroot loopback mapping, including collision-safe cleanup and rejection of duplicate service ports.
- [ ] 1.4 Migrate Ollama and Agentgateway callers to the neutral API without changing their public CLI, configuration, lifecycle, or port-scoped firewall behavior.
- [ ] 1.5 Update focused forwarding tests to prove every runtime uses one hostname mapping or Apple DNS entry while Ollama and Agentgateway remain independently routed by port.

## 2. Parse and Validate Internet Proxy Configuration

- [ ] 2.1 Add `--internet-proxy URL` and an Internet-proxy module that accepts only credential-free HTTP URLs on `127.0.0.1`, `localhost`, or `::1` with an explicit valid port and no query or fragment.
- [ ] 2.2 Add early conflict validation for `--no-firewall`, `--extra-domain`, and `--allow-github`, with errors explaining bypass prevention and external destination-policy ownership.
- [ ] 2.3 Construct the forwarded `host.docker.internal` URL while preserving the configured port, with no Pipelock- or Smokescreen-specific behavior.
- [ ] 2.4 Add a bounded host-loopback listener preflight that runs before real sandbox startup, fails actionably, requires no public destination, and is never invoked in dry-run.
- [ ] 2.5 Add CLI tests for valid and invalid URLs, all option conflicts, absent-option no-op behavior, preflight success/failure ordering, and dry-run network isolation.

## 3. Plan Multiple Independent Local Services

- [ ] 3.1 Refactor CLI planning to build and manage an ordered collection of forwarding plans for Internet proxy, Agentgateway, and Ollama while deduplicating their shared `host.docker.internal` runtime mapping and owned-resource cleanup.
- [ ] 3.2 Generate canonical proxy environment values for `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` using the forwarded Internet-proxy URL.
- [ ] 3.3 Generate identical `NO_PROXY` and `no_proxy` lists with loopback literals and one `host.docker.internal` entry when Agentgateway, Ollama, or another local service must bypass proxy routing, and test that ordinary Internet destinations still use the configured proxy.
- [ ] 3.4 Feed the same generated environment into container and chroot argv plus interactive, headless, Bash, and inherited agent execution paths.
- [ ] 3.5 Extend sanitized dry-run output to preview proxy and bypass variables and forwarding plans without writes, runtime starts, secret reads, or network calls.
- [ ] 3.6 Add environment and session tests for uppercase/lowercase variables, all launch modes, shared-hostname deduplication, Agentgateway coexistence, and Ollama coexistence.

## 4. Enforce Proxy-Only Firewall Egress

- [ ] 4.1 Replace service-specific firewall scalar inputs with typed local TCP destinations and preserve existing normal-mode Ollama and Agentgateway rendering through compatibility-focused tests.
- [ ] 4.2 Add an explicit Internet-proxy firewall policy that retains loopback, ESTABLISHED/RELATED, required narrow ICMPv6 handling, general DNS denial, and default INPUT/FORWARD/OUTPUT DROP behavior.
- [ ] 4.3 Resolve or pin `host.docker.internal` once and render separate exact address-and-port TCP ACCEPT rules for the Internet proxy and each enabled Agentgateway, Ollama, or other supported local service.
- [ ] 4.4 Ensure proxy mode creates no ordinary public IPv4/IPv6 ipset entries and admits no provider domains, GitHub ranges, extra domains, resolver access, or broad devcontainer host-gateway access.
- [ ] 4.5 Make inability to establish the intended IPv6 policy fatal in Internet-proxy mode and verify that direct IPv4, direct IPv6, and direct DNS paths remain denied.
- [ ] 4.6 Add renderer and integration tests for one shared hostname across Docker, Podman, Apple `container`, Linux bridge, and chroot; proxy-only port scoping; absence of public allowlists; dual-stack fail-closed behavior; normal-mode compatibility; and simultaneous local-service destinations.

## 5. Document Operations and Security Boundaries

- [ ] 5.1 Add `docs/internet-proxy.md` with setup references to `internet-proxy-locally`, primary Docker and Apple `container` usage, and no Docker Compose examples.
- [ ] 5.2 Document iptables bypass prevention, external Internet destination-policy ownership, Agentgateway AI/MCP credential isolation, routing-hint limitations, and non-proxy-aware application failure.
- [ ] 5.3 Document independent runtime failure behavior and explicit non-goals, including no proxy lifecycle management, transparent interception, TLS interception, CA installation, policy synchronization, or AI/MCP rerouting.
- [ ] 5.4 Add a concise feature pointer to the user-facing documentation without expanding `README.md` into the full operational guide.

## 6. Verify Acceptance and Regression Safety

- [ ] 6.1 Add or document an external-setup acceptance procedure for permitted and denied HTTP/HTTPS destinations, including private, link-local, metadata, IPv4, and IPv6 cases.
- [ ] 6.2 Verify bypass prevention with `curl --noproxy '*'`, unset proxy variables, arbitrary public-IP TCP, and direct DNS attempts from inside the sandbox.
- [ ] 6.3 Verify Agentgateway and Internet proxy failure independence in both stop orders and verify Ollama bypass behavior when enabled.
- [ ] 6.4 Run `uv run python -m compileall src tests` and `uv run pytest -q`, then record any runtime-dependent Docker and Apple `container` acceptance checks that require the external proxy setup.
