# Tasks

## 1. Endpoint Parsing and Local/Remote Planning

- [x] 1.1 Introduce shared typed endpoint parsing and classification; verify HTTP loopback, remote HTTP/HTTPS, default ports, API paths, DNS names, IPv4/IPv6, and mapped-loopback cases with unit tests.
- [x] 1.2 Reject local HTTPS, malformed authorities, userinfo including empty userinfo, invalid ports, query/fragment, forbidden Internet paths, wildcard/multicast/scoped addresses, and unsafe hostname characters; verify early failures without secrets, network, or generated-file writes.
- [x] 1.3 Migrate both proxies to effective sandbox URLs and select forwarding only for local services; verify existing runtime strategies, remote-only sessions without host alias setup, mixed endpoints, remote services sharing a port, local collision detection, and Ollama compatibility.
- [x] 1.4 Update CLI help and proxy documentation with the accepted URL matrix and independent-proxy examples; verify documented invocations through non-mutating dry-run and confirm omitted ports display correctly.

## 2. Destination-Specific Firewall Enforcement

- [x] 2.1 Generalize firewall inputs and templates to independently resolve and pin each destination using sandbox-visible resolution before DNS closure; verify exact address/port pairs, hostname deduplication, literal addresses, and multiple A/AAAA answers in renderer tests.
- [x] 2.2 Validate resolution results, reject prohibited address classes, and handle address-family enforcement failures; verify unresolved names, remote names resolving to loopback, IPv6-only endpoints, and Internet-mode IPv6 enforcement failure abort before agent execution.
- [x] 2.3 Preserve Internet-mode conflicts and default-deny policy, agent-only explicit domain options, and no-firewall behavior; verify no accidental provider/GitHub/general DNS/broad gateway permissions in session and devcontainer templates.
- [x] 2.4 Document session DNS pinning, restart requirements, sandbox reachability, and the IP/port rather than virtual-host firewall boundary in `docs/security.md` and proxy docs; verify text matches rendered policy and contains no VPN-specific setup requirement.

## 3. Independent Routing and Agent Configuration

- [x] 3.1 Generate canonical proxy variables and deduplicated direct-service bypass values for local/remote combinations; verify upper/lowercase consistency, policy precedence over injected variables, loopback entries, remote gateway bypass, and local Ollama bypass.
- [x] 3.2 Carry effective gateway URLs into Pi, OpenCode, and interactive/headless Bash configuration; verify remote scheme/hostname/API path preservation, selected model behavior, existing bearer-key isolation, and no Internet-proxy credential forwarding.
- [x] 3.3 Ensure Internet-proxy-only sessions require no gateway discovery or key and retain ordinary provider credentials; verify neither/either/both option combinations and existing HTTP loopback behavior in CLI/session tests.
- [x] 3.4 Update agent and Internet proxy usage examples and failure-independence guidance; verify each supported invocation via dry-run without gateway secret lookup or network access.

## 4. TLS Trust and Endpoint Preflight

- [x] 4.1 Allow public CA inputs with either proxy, retain CA-only validation, and load supplied trust into host preflight TLS contexts without changing host trust stores; verify agent-only private-CA discovery and malformed/private-key/leaf-certificate rejection.
- [x] 4.2 Reuse CA installation for generated sandbox and devcontainer images; verify readable trust artifacts, relevant client trust configuration, rebuild/cache behavior, and documented `--no-build` limitations.
- [x] 4.3 Make gateway discovery direct regardless of ambient host proxy variables and keep redirects disabled; verify normal catalog/auth failures plus redirect rejection, CA validation, expiry/name mismatch, and absence of bearer-key leaks using controlled fixtures.
- [x] 4.4 Add TCP preflight for HTTP and verified TLS handshakes for HTTPS Internet proxies on the host and through the final sandbox policy; verify bounded failure behavior, no public destination dependency, and host-success/sandbox-failure handling.
- [x] 4.5 Update final gateway probes to use effective scheme, authority, and path with explicit direct routing; verify startup blocks on sandbox TLS/connect failure and remote diagnostics omit Apple localhost remediation.
- [x] 4.6 Document TLS-to-proxy versus destination TLS, private CA scope, absent local HTTPS/client certificates/Internet-proxy authentication, and no downgrade/fallback behavior; verify docs against certificate and HTTP 407 tests and record deferred authentication/DNS refresh in `TODO.md`.

## 5. Integrated Acceptance and Spec Completion

- [x] 5.1 Exercise dry-run for standalone remote proxies, both remote proxies, mixed local/remote endpoints, and private CAs; verify no DNS, secret reads, file writes, container starts, or forwarding resources and accurate endpoint/firewall previews.
- [x] 5.2 Add controlled end-to-end fixtures for remote HTTP/HTTPS Internet proxy requests and gateway requests; verify curl and representative bundled client paths, HTTP requests and CONNECT, public/default trust where available, private CAs, and unsupported-client fail-closed behavior; record tested versions and limitations in `docs/development.md` and proxy docs.
- [x] 5.3 Extend isolation acceptance to direct IP/DNS attempts, unset proxy variables, `--noproxy '*'`, cross-endpoint port attempts, and both proxy stop orders; verify permitted endpoint traffic remains independent and direct fallback remains blocked, with local Ollama checked separately.
- [x] 5.4 Run `uv run python -m compileall src tests`, `uv run ty check`, and `uv run pytest -q`; record results and separately identify any Docker/Podman/Apple runtime acceptance checks unavailable in the execution environment.
- [x] 5.5 Reconcile the existing remote-proxy ROADMAP section with the delivered feature, update local-only capability purposes when syncing, and validate this change with `openspec validate generalize-proxy-endpoints --strict`; verify final deltas apply against the archived Internet-proxy baseline without lost scenarios.

## Verification record (2026-10-14)

- `uv run python -m compileall src tests`: passed.
- `uv run ty check`: passed.
- `uv run ruff check src tests scripts/e2e-internet-proxy-isolation.py`: passed.
- `uv run pytest -q`: 722 passed, 4 skipped, 414 subtests passed.
- `openspec validate generalize-proxy-endpoints --strict`: passed.
- Every scenario in a modified requirement is retained from the main-spec baseline; untouched requirements remain unchanged at sync. Agent/Internet purpose text is supplied in the deltas for the later sync/archive step; this apply run does not archive the change.
- Controlled HTTP/TLS fixtures and generated resolution/probe execution passed. See `docs/development.md` for client versions and the exact scope of these checks.
- Docker, Podman, and Apple container are unavailable here, as are network-administration capabilities. Actual runtime firewall/bypass enforcement, IPv6 routing, service-stop independence, local Ollama forwarding, and pinned Pi/OpenCode client acceptance are not claimed as executed. The acceptance harness is extended and this remaining runtime matrix is recorded in `TODO.md`.
