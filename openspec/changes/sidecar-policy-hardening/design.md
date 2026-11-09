# Design

## Context

- **`squid.conf.j2`:**
  - `allowed` is one `dstdomain` ACL. `squid_config._squid_domain` renders `*.x` as `.x`, which Squid matches against `x` and every subdomain.
  - Plain HTTP is admitted for every allowed host on port 80 (`http_access allow plain_http`).
  - Copilot interception uses `urlpath_regex -i ^/copilot_internal/`, with no method restriction.
  - `host_verify_strict on` is set. `strip_query_terms` is not set, and Squid's default is on.
  - There's no `client_lifetime` (default 1 day).
  - In upstream mode, `private_dst` isn't rendered.
- **`egress_policy`:**
  - `normalize_host_pattern` accepts `*.` with a base of at least two labels.
  - `_dedupe` drops hosts a wildcard covers, including the apex.
  - `GITHUB_REPOSITORY_HOSTS` has 11 entries.
  - `effective()` always adds `PROVIDER_HOSTS`, which becomes the union of per-agent sets in `scope-firewall-allowlist`.
- **Reload** (`sbx-reload`) uses `squid -k reconfigure`, which keeps established tunnels. Restart (`egress_sidecar.restart`) reuses a valid CA.

## Goals / Non-Goals

**Goals:**
- Sidecar defaults match the minimal posture: HTTPS only, precise wildcards, precise Copilot access.
- Guarantees in the spec and docs say exactly what is enforced.

**Non-Goals:**
- Per-session sidecar policy (ROADMAP).
- Inspecting spliced TLS. Splicing is deliberate, and its limits are documented.
- Local destination resolution in upstream mode. That stays hostname-based by design. The parent is responsible, and the CLI now says so.

## Decisions

### 1. Separate exact and wildcard ACLs

```
acl allowed_exact dstdomain api.anthropic.com pypi.org …
acl allowed_wild  dstdom_regex -i \.npmjs\.org$ \.example\.com$ …
acl allowed any-of allowed_exact allowed_wild
```

`*.example.com` becomes the regex `\.example\.com$`, with the base escaped by `re.escape`. That requires at least one label before the base, so the apex doesn't match. `_dedupe` stops dropping an apex that a wildcard used to cover. The same pair of ACLs exists for `http_allow` (`http_exact`, `http_wild`).

**Public suffix check:**
- a bundled `public_suffix_list.dat` snapshot (ICANN and private sections), with a dated header;
- a small matcher implementing the PSL rules (`*` and `!` exceptions);
- `normalize_host_pattern` rejects a wildcard whose base is a public suffix;
- `scripts/update-pins.py` refreshes the snapshot.

Bundling instead of a runtime dependency keeps validation offline and pinned.

### 2. HTTPS-only default with an explicit list

```
http_access deny plain_http !http_allowed page_plain_http_not_allowed   # new cause
http_access deny plain_http !http_port80 page_port_not_allowed
… existing ip-literal / host / private rules …
http_access allow plain_http http_allowed
```

`http_allow` entries must also be in `allow` (or be covered by an `allow` wildcard), and must not be provider hosts or GitHub repository hosts. The policy loader enforces this.

### 3. Copilot: exact pairs and normalization denials

The `copilot-egress` capture (`scripts/e2e-copilot-egress.py`) is extended to record methods, and writes the observed pairs into `egress_policy.COPILOT_API_REQUESTS`, currently `(("GET", "/copilot_internal/user"), ("GET", "/copilot_internal/managed_settings"))`. Rendered:

```
acl copilot_get    method GET
acl copilot_paths  urlpath_regex ^/copilot_internal/(user|managed_settings)(\?.*)?$
acl path_tricks    urlpath_regex -i /\.\.?(/|\?|$) %2e %2f %5c
acl api_host_hdr   req_header Host -i ^api\.github\.com(:443)?$
http_access deny intercepted path_tricks page_path_not_allowed
http_access deny intercepted !api_host_hdr page_path_not_allowed
http_access allow intercepted copilot_get copilot_paths
http_access deny intercepted page_path_not_allowed
```

Whether Squid normalizes paths before `urlpath_regex`, and how it treats a `Host` header that disagrees with the CONNECT host on bumped requests, is checked by fixture tests against a running sidecar (task 3.2). The explicit denials stay in place regardless.

### 4. Implicit providers are configurable

`[egress] providers = "all" | ["claude", …] | []` selects which per-agent sets `effective()` adds. Before the sandbox starts, `_egress_sidecar_policy` refuses a launch whose agent isn't covered, unless `--agent-proxy` is used.

### 5. Revocation

- `client_lifetime {{ tunnel_lifetime }}`, default `1 hour`, configured by `[egress.sidecar] tunnel_lifetime` (Squid time units, from 5 minutes to 24 hours).
- `allow remove --terminate` edits the policy (with `serialize-egress-policy-updates`' locking), then calls `egress_sidecar.restart`.
- The confirmation prompt counts running sidecar-mode sessions from the session records. `--yes` skips it, and a non-interactive stdin without `--yes` refuses.

### 6. Logs

- `strip_query_terms on` is rendered explicitly.
- The log format contains no `%>h`/`%<h` header codes, and a renderer test guards that.
- Docker: pass `--log-opt max-size=10m --log-opt max-file=3` when `docker info` reports the `json-file` or `local` log driver; otherwise warn.
- Podman: `--log-opt max-size=10m`.
- Apple `container`: no option exists, so the docs say its logs are unbounded and suggest `project-sandbox-egress restart` to rotate.

### 7. Upstream notice and splice documentation

- The notice text is shared by `status`, the launch path and `doctor`.
- `docs/egress-sidecar.md` gains a "What splicing can't see" section, covering:
  - encrypted `Host` and `:authority`;
  - domain fronting through CDN-hosted allowed names;
  - HTTP/2 connection coalescing;
  - sidecar mode as the strongest *hostname* control, not a content control.
- An opt-in e2e test uses a controlled origin behind a fronting-capable CDN, when configured through environment variables, to demonstrate the limit.

## Risks / Trade-offs

- [Breaking plain-HTTP users, such as apt mirrors over HTTP] → A clear `plain-http-not-allowed` page naming `http_allow`, and release notes.
- [Apex no longer covered by wildcards] → Policy load warns once when a wildcard's apex isn't listed, to aid migration.
- [A narrower Copilot surface breaks after a CLI update] → The `copilot-egress` suite already exists to re-capture, and is now part of the agent-version bump checklist.
- [One-hour tunnel lifetime closes long streams] → Agent API calls are short-lived. The lifetime is configurable.
- [PSL snapshot ages] → It's refreshed with the other pins. An outdated list errs on the side of accepting a newly added suffix's wildcard.

## Migration Plan

- Existing `egress.toml` files keep loading.
- Public-suffix wildcards now fail at load, with a message naming them.
- Wildcard-only hosts that relied on apex coverage get a warning.
- The first reload after upgrading applies HTTPS-only, and users with HTTP needs add `http_allow`.
