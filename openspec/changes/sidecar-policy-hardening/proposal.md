# Proposal

## Why

Sidecar mode is the hostname-level egress option. The 2026-11-08 security review's networking recommendations found several places where its policy is broader, or its guarantees vaguer, than the docs suggest:

- **Plain HTTP.** Every allowlisted host, including the agent providers, is reachable over plain HTTP on port 80 (`squid.conf.j2`, `http_access allow plain_http`). That allows plaintext leakage, and origin impersonation by anyone on the path.
- **Wildcards.**
  - `*.example.com` is rendered as Squid's `.example.com`, which also matches the apex `example.com`.
  - The two-label check accepts public-suffix wildcards such as `*.co.uk`, `*.github.io` and `*.amazonaws.com`. Each admits every tenant under that suffix.
- **GitHub exclusions.** The repository-host denylist is finite, and misses known transports such as `lfs.github.com`. The spec phrases it as blocking repository access in general, which a fixed list can't guarantee.
- **Copilot interception.** `^/copilot_internal/` admits any method and any path under that prefix on `api.github.com`. Dot segments, backslashes and encoded separators are already refused on this line (`554a7b8`, live-tested by `test_copilot_paths`); a `Host` header that disagrees with the CONNECT host is not.
- **Default providers.** Every agent's provider hosts are always admitted, and a user can't remove them, even when the user wants no implicit providers at all.
- **Revocation.** `allow remove` reloads Squid, but tunnels already spliced to a revoked host stay open for Squid's default `client_lifetime` (one day).
- **Upstream mode.** With a parent proxy, the private-address denylist is deliberately skipped, and the parent decides. Nothing tells the user that the parent must then enforce internal and metadata restrictions.
- **Logs.** Access logs go to the runtime's container log, with the runtime's retention (unbounded `json-file` on Docker by default). URL redaction relies on Squid's defaults rather than an explicit setting.
- **TLS splice limits.** For spliced connections, the sidecar verifies that the SNI equals the CONNECT host, but can't see the encrypted HTTP `Host`/`:authority`, so domain fronting through an allowed CDN host isn't prevented. This isn't documented.

## What Changes

- **HTTPS only by default.** Plain HTTP is denied for every host unless it's listed in `[egress] http_allow` (exact hosts or wildcards, validated like `allow`). Agent provider hosts can never be added to `http_allow`. Denials are logged with the new cause `plain-http-not-allowed`.
- **Wildcards mean subdomains only.** `*.example.com` matches `a.example.com` and `a.b.example.com`, not `example.com`, which must be listed separately.
- **No public-suffix wildcards.** A wildcard is rejected in `egress.toml` and in `allow add` when its base is a public suffix, or when a public suffix lies beneath its base (for example `*.amazonaws.com`, which would cover every tenant of `s3.amazonaws.com`). Both the ICANN and the private section of the Public Suffix List count. A dated PSL snapshot is bundled and refreshed by `scripts/update-pins.py`.
- **GitHub denylist and wording.**
  - Known repository transport hosts are added: `lfs.github.com`, `github-releases.githubusercontent.com`, `objects-origin.githubusercontent.com`, `pkg-containers.githubusercontent.com`, and any others the verification in task 1.2 finds.
  - The spec now says the sidecar blocks *the listed GitHub repository hosts*. It no longer claims to block all repository content, since user content can be served from other domains.
- **Copilot path narrowing.** Interception of `api.github.com` admits only the exact method and path pairs that the `copilot-egress` capture recorded (today `GET /copilot_internal/user` and `GET /copilot_internal/managed_settings`, with optional query). It denies any request whose raw path contains dot segments, encoded `.`, `/` or `\`, or a `Host` header that differs from the CONNECT host.
- **Configurable implicit providers.** `[egress] providers` is `"all"` (default: the union of every agent's set), a list of agent names, or `[]` (none). A sidecar-mode launch of an agent whose providers the policy excludes fails with a clear message, unless an agent proxy is used.
- **Bounded tunnels and explicit revocation.**
  - `client_lifetime` defaults to 1 hour (`[egress.sidecar] tunnel_lifetime`), so a revoked host can't keep a tunnel open indefinitely.
  - `project-sandbox-egress allow remove --terminate` also restarts Squid, closing every existing tunnel, after asking for confirmation if sessions are running.
  - Without the flag, the command says that existing tunnels to the host stay open until they close or reach the lifetime.
- **Upstream mode warning.** `status`, and every sidecar-mode launch with a parent, state that the parent must enforce private-address and metadata restrictions. The `doctor` check reports it.
- **Logs.** The rendered config sets `strip_query_terms on` explicitly and never logs request headers. The sidecar container runs with bounded log rotation where the runtime supports it (Docker/Podman `--log-opt max-size=10m,max-file=3`). Apple `container` logs are documented as unbounded.
- **Documentation** of the splice limits (no visibility into encrypted `Host`, so domain fronting is possible through CDN-fronted allowed hosts), plus a domain-fronting test against a controlled origin.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `egress-sidecar`:
  - "Destinations are filtered by hostname with SNI verification": HTTPS-only default, subdomain-only wildcards, the public-suffix rule.
  - "GitHub repository access is blocked in sidecar mode": a longer host list, and wording limited to the listed hosts.
  - "Copilot access is a separate host profile": exact method and path pairs, plus normalization denials.
  - "The effective allowlist adds agent provider hosts": configurable implicit providers.
  - "Unsafe destinations are denied before any upstream connection", "GitHub repository hosts cannot be re-admitted" and "The Copilot profile is verified by a capture suite": mixed DNS answers, the new content hosts, recorded methods.
  - Added: public-suffix wildcards, `http_allow`, GitHub content hosts and ambiguous intercepted requests, alongside the requirements below.
  - "Denials are observable": the new `plain-http-not-allowed` cause.
  - Added requirements: tunnel lifetime and revocation, the upstream-mode warning, log bounds and redaction.
- `egress-sidecar-management`: added: `allow remove --terminate`.

## Impact

- **Code:**
  - `templates/egress-sidecar/squid.conf.j2`: HTTP rules, the wildcard regex ACL, Copilot method and path ACLs, normalization denials, `client_lifetime`, `strip_query_terms`.
  - `squid_config.py`.
  - `egress_policy.py`: `http_allow`, `providers`, PSL check, GitHub host list.
  - `egress_sidecar.py`: log options, restart for `--terminate`, upstream warning.
  - `egress_cli.py`: `--terminate`, status.
  - `doctor.py`.
  - `scripts/update-pins.py`: PSL snapshot.
  - `scripts/e2e-copilot-egress.py`: re-capture of methods.
- **BREAKING:**
  - plain-HTTP destinations need `http_allow`;
  - `*.x` no longer admits `x`;
  - public-suffix wildcards are rejected;
  - Copilot paths outside the captured set are denied;
  - tunnels older than an hour are closed.
- **Depends on:** `scope-firewall-allowlist` (the per-agent provider table). The policy edit lock `--terminate` uses already exists on this line (`0224362`).
- **Origin:** ported from the `sec-fixing-claude` branch's draft; the live security suite's XFAILs (`test_spliced_domain_fronting_and_http_default`, `test_copilot_host_sni_methods_and_redirect`, `test_wildcard_apex_public_suffix_and_implicit_providers`, `test_revocation_existing_tunnel_vs_new_connection`) become passing checks.
- **Docs:** `docs/egress-sidecar.md`, `docs/security.md`.
