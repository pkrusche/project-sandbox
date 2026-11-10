# TODO - outstanding items for next release

Verification tasks below have runnable cases in the [security verification
suite](docs/security-verification.md). Live runtime results are still pending;
adding a test does not close the task or implement the proposed hardening.

- Generated Cargo builds: verify literal filename COPY behavior on Docker,
  Podman, and Apple container with spaces, quotes, dollar signs, and wildcard
  characters.

- Firewall initialization: verify the one-time guard and sudo revocation in
  live Docker, Podman, and Apple containers, including workspace import-shadowing
  attempts. Recreate containers from rebuilt images before testing.

- Policy edit serialization: verify concurrent cross-runtime edits and delayed
  launches after revocation with live Squid on Docker, Podman, and Apple
  container.

- Build/session isolation: verify concurrent launches, image-ID execution, and
  Apple session-alias retention/deletion on live Docker, Podman, and Apple
  container. Add orphan alias cleanup that checks live containers after SIGKILL.
  Verify private session input mounts and concurrent provisioning on each runtime.
  Add recovery for orphan session-input trees after SIGKILL or host crashes,
  checking live containers before removing any credentials or prompts.

- Workspace leases: verify same-branch rejection on live runtimes. Add persistent
  ownership/runtime reconciliation after SIGKILL or host crashes so an orphan
  container cannot outlive its host lease; stop orphan containers before reuse.

- Private mount storage: add orphan-mask/helper garbage collection that accounts
  for live containers and persistent devcontainer configurations after SIGKILL.
  Verify private mounts and devcontainer reopen on live Docker, Podman, and Apple
  container runtimes. Extend the understood bind-mount options where safe.

- VCS access: run the `vcs-access` e2e suite on Docker, Podman and Apple
  container; extend coverage to unlink/rename attempts and branch host capture
  after exit. Add Apple support for Git pointer-file layouts, nested
  repositories/submodules, and lifecycle cleanup for old devcontainer masks.
  Consider reserving absent metadata paths without modifying the host checkout.

- Run the automated sole-owner crash test to verify that `kill -9` of the only
  `--pi-ollama` session releases the shared bridge forwarder (it should exit within about one poll interval) and
  that a fresh session then attaches cleanly.

## Networking allowlist recommendations

Source paths below are relative to `src/project_sandbox/`.

| Area | Current behavior and recommendation |
| --- | --- |
| Firewall-mode destination identity | `templates/init-firewall.sh.j2:168–225,316–317` resolves names once and admits the resulting IPs. The final rules have no protocol or destination-port restriction, apart from earlier DNS drops. Any service/virtual host on those IPs is potentially reachable. Restrict normal service rules to required TCP ports, maintain explicit exceptions, and use sidecar mode where hostname enforcement is required. Do not describe IP pinning as a hostname guarantee. |
| Private addresses in normal domain pins | The normal A/AAAA loop adds DNS answers without excluding loopback, RFC1918, link-local, metadata, or other special-use destinations. A compromised resolver or an explicitly added domain resolving internally can open unintended internal services on all ports. Apply address classification before inserting pins and require a separate explicit private-service exception with exact ports. Keep intentional remote corporate proxy endpoints distinct from public provider policy. |
| GitHub/Copilot breadth | `--allow-github` imports broad web/API/git CIDRs and adds repository, registry (`ghcr.io`), Copilot, and telemetry hosts. Selecting Copilot implies this broad access in firewall mode. Separate Copilot inference/entitlement, repository access, registries, and telemetry into independently enabled profiles; prefer the existing restricted sidecar Copilot profile where supported. Keep the warning about broad GitHub-token scope. |
| Default providers | `egress_policy.effective()` always adds all seven provider hosts, even for a gateway-backed session; users cannot remove them. Normal firewall mode also admits both provider families unless an agent proxy suppresses them. Allow an explicitly restrictive policy with no implicit providers, or isolate sidecars by policy/profile if per-session restrictions are needed. Make the global-union semantics visible in previews. |
| Devcontainer gateway | `templates/init-firewall.sh.j2:244–258` admits every port/protocol to and from the gateway. This exposes host services and precedes the port-53 drops, so it can also permit gateway DNS despite the general-DNS-block claim. Make broad gateway access opt-in; list the required IDE connections and implement narrower, stateful rules. Test DNS and unexpected host ports in this mode. |
| Sidecar TLS scope | The sidecar checks CONNECT host against ClientHello SNI, then splices most connections. It cannot inspect encrypted HTTP Host/`:authority`, paths, methods, or payloads on those connections. That follows from [Squid's documented splice behavior](https://www.squid-cache.org/Doc/config/ssl_bump/). Document this limit; test domain fronting with a controlled origin that supports it. This review did not establish that a currently allowlisted provider supports fronting. |
| Sidecar HTTP | `templates/egress-sidecar/squid.conf.j2` permits plain HTTP on port 80 for most allowed hosts, including providers. Prefer HTTPS-only defaults with explicit HTTP exceptions to reduce plaintext leakage and origin impersonation. |
| Wildcards and GitHub exclusions | `*.example.com` also matches the apex after Squid conversion. Two-label validation still admits broad public-suffix patterns such as `*.co.uk`. Reject public-suffix wildcards or require explicit acknowledgement. The finite repository-host denylist is not a complete GitHub-content classifier: `lfs.github.com` is in the firewall list but absent from `GITHUB_REPOSITORY_HOSTS`, and user content can live on other domains. Clarify the guarantee and test every officially supported repository transport/host before claiming all repository access is blocked. |
| Copilot interception | `/copilot_internal/` permits any matching path and method. Narrow to observed entitlement endpoints/methods where practical. Add live fixture tests for dot segments, percent encoding, conflicting Host/CONNECT/SNI, and redirects. These are validation tasks, not confirmed path bypasses. |
| Private destinations and upstream parents | Direct Squid mode has a useful private-address denylist; upstream mode deliberately omits it and trusts the parent for destination resolution. Require the parent to enforce equivalent internal/metadata restrictions when those are part of the threat model. Test mixed public/private DNS answers, rebinding, IPv4-mapped IPv6, and deployment-specific NAT64 paths. |
| Startup and revocation | The initial installation still flushes rules before installing final DROP policies and allows ESTABLISHED/RELATED traffic; sidecar reloads also need explicit semantics for existing tunnels. Install policy atomically where possible and distinguish blocking new connections from terminating existing ones after revocation. Audit broadly allowed outbound ICMPv6 error types alongside the actual capabilities of the agent process. No packet-level bypass was tested here. |

## Additional hardening

- **Mount encoding:** CLI bind mounts use CSV encoding, but devcontainer mount
  strings still need consistent runtime encoding. Verify live parse/render round trips for unusual project names (including commas, quotes,
  dollars, spaces, and wildcards) on each runtime, and fail closed for unsupported
  control characters and mount forms. Keep raw user mount parsing aligned with
  runtime grammar; JSON or argv escaping alone does not cover nested syntax.
- **Host lock files:** `host_locks.path_lock()` correctly uses `O_NOFOLLOW` and
  avoids truncation, but does not verify the opened inode is a regular file
  owned by the caller. Names are predictable in shared temporary storage. Use a
  verified private per-user lock directory, `fstat()` checks, and bounded waits
  with useful diagnostics. Existing OS protections may limit cross-user attacks;
  do not depend on identical behavior across Linux and macOS. Never unlink a
  live lock file to recover it, since that can create two independently locked
  inodes.
- **Sensitive logs:** Squid's `%ru` logging includes URLs/query strings for
  visible HTTP and intercepted requests; redact sensitive fields and define
  retention/access rules. Token substitution in logs is useful but is not a
  general data-loss prevention boundary.
- **Supply chain:** exact top-level npm versions are useful, but global
  `npm install -g package@version` does not freeze all transitive dependency
  resolution. Use reviewed lockfiles/integrity metadata or reproducible tool
  layers, and define a deliberate image/security-update cadence. Project build
  scripts and user-supplied Dockerfiles remain trusted image-producing code.
