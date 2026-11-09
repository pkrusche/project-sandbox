# Proposal

## Why

Firewall mode admits much more than a session needs (TODO.md networking table):

- **Every provider for every session.** All seven provider hosts (Anthropic and OpenAI families) are admitted regardless of agent. A headless Claude run can reach `chatgpt.com`. Some provider hosts sit on shared CDN addresses, so each extra host also admits unrelated sites on those IPs.
- **All ports and protocols.** Pinned addresses are accepted on every port and protocol, so any service on those IPs is reachable.
- **Private addresses get pinned.** DNS answers aren't classified. A domain resolving to loopback, RFC 1918, link-local, metadata (`169.254.169.254`) or other non-global ranges opens that internal address on all ports.
- **`--allow-github` is broad, and Copilot implies it.** It admits GitHub's web, API and git CIDRs, repository hosts, `ghcr.io` and telemetry hosts, and `--agent copilot` turns it on. Firewall mode can only admit `api.github.com:443` whole, so it can't restrict Copilot to the `/copilot_internal/` paths the CLI needs. The sidecar's Copilot profile can. Sandboxes don't need GitHub repository access.
- **The firewall is baked into the image.** The rendered firewall script is `COPY`ed into the image, so any change to the allowlist means another image. Scoping the allowlist per agent would make every agent switch a rebuild. It also produces stale-policy images (TODO.md item 1).

Decisions made with the user (2026-11-09):
- headless sessions, and interactive sessions with a named agent, admit only that agent's hosts, while a plain shell session (`--agent bash`) gets every provider;
- align the allowlist with the sidecar's minimal provider set;
- no GitHub repository access, with Copilot only through the sidecar.

## What Changes

- **The firewall becomes a per-session runtime input.**
  - The firewall script is rendered into the session's private staging directory and mounted read-only at `/project-sandbox-firewall/`.
  - The root stage (`isolate-firewall-helpers`) runs it from there.
  - Images no longer contain any network policy. Changing agent, domains, proxies or Ollama never rebuilds the image.
  - A session that needs a firewall and has no mounted script fails closed.
- **Provider hosts are scoped to the agent.**
  - One table maps each agent to its provider hosts: Claude to the Anthropic hosts; Codex to the OpenAI hosts; OpenCode and Pi to the hosts of their configured provider families, or none with `--pi-ollama` or an agent proxy.
  - `--agent bash` admits the union of all of them. The sidecar's always-allowed provider set is defined as that same union, so the two modes can't drift.
  - The initial table is the current split. Task 1 captures each agent's real minimal set (headless and login flows) through the sidecar's access log, and removes hosts no agent needs, notably CDN-fronted login hosts in headless sessions.
- **TCP 443 only, unless a port is given.**
  - Provider and extra-domain pins are admitted as address and port pairs: TCP 443 by default.
  - `--extra-domain host:port` admits another TCP port for that host.
  - DNS stays blocked, and local services keep their exact port-scoped rules.
- **Non-global addresses are never pinned implicitly.**
  - DNS answers for provider and extra domains are classified. Non-global answers are dropped with a warning: loopback, private, link-local, CGNAT, documentation, multicast, unspecified, IPv4-mapped and NAT64-embedded equivalents.
  - `--allow-private-destination host:port` is the explicit exception, for internal mirrors or proxies, and admits only that host and port.
- **No GitHub in firewall mode.**
  - **BREAKING:** `--agent copilot` requires `--egress sidecar`, and is refused in firewall mode with a pointer to the sidecar Copilot profile.
  - **BREAKING:** GHE.com tenants (`--copilot-host <tenant>.ghe.com`) become unavailable until the sidecar gains a tenant profile (ROADMAP).
  - **BREAKING:** `--allow-github` is deprecated. It is still accepted, because project-sandbox-tui passes it, but it is ignored, with a warning that suggests `--extra-domain` for specific hosts.
  - The GitHub meta CIDR import, along with its root-run `curl`/`jq` step, is removed.

## Capabilities

### New Capabilities

- `firewall-egress-policy`: what firewall-mode egress admits: per-agent provider scoping, port scoping, address classification and the private-destination exception, `--extra-domain` syntax, `--allow-github` deprecation, and delivery of the firewall as a per-session runtime input.

### Modified Capabilities

- `copilot-agent-support`: "Copilot implies GitHub network access, chosen explicitly" is replaced by "Copilot requires sidecar egress". Tenant hosts are refused until the sidecar supports them.
- `pi-agent-support`: "Pi provider allowlist warning" names `--extra-domain` only, not `--allow-github`.

## Impact

- **Code:**
  - `templates/init-firewall.sh.j2`: the provider list and GitHub block are removed; address/port sets; resolution moved into the isolated Python helper with classification.
  - `templates/_resolve_endpoints.py.j2`: extended for domains.
  - `firewall.py`: per-agent hosts, ports, private exceptions.
  - `egress_policy.py`: `PROVIDER_HOST_SETS`, with `PROVIDER_HOSTS` as their union.
  - `templates/Dockerfile.j2`: the firewall `COPY` is removed.
  - `cli.py`: the firewall mount, Copilot and tenant refusals, `--allow-github` deprecation, `--extra-domain host:port`, `--allow-private-destination`.
  - `container_cli.py`: the mount.
- **Interplay:**
  - `isolate-firewall-helpers`: the root stage runs `/project-sandbox-firewall/init-firewall.sh` instead of a baked path. Its `-I`, `cd /` and `env -i` protections apply unchanged.
  - `pin-launch-image-identity`: the `settings` label loses its network fields, and TODO.md item 1 can no longer occur, because Ollama rules are runtime input.
  - `validate-all-mount-sources` already forbids user targets under `/project-sandbox-*`.
  - `private-session-state` provides the private staging the script is mounted from.
- **Users:**
  - Headless sessions lose access to other agents' providers.
  - Extra domains are 443-only unless a port is given.
  - Copilot needs the sidecar.
  - `--allow-github` no longer opens GitHub.
  - project-sandbox-tui keeps working (it passes `--allow-github`, `--extra-domain=…` and `--agent=…`), and a TUI release can drop its `allow_github` option later.
- **Docs:** `docs/security.md` (per-agent sets, ports, classification, IP-pinning limits on shared CDNs, sidecar union semantics), `docs/usage.md` (flags), Copilot docs (sidecar only).
- **Out of scope:** sidecar policy changes (`sidecar-policy-hardening`) and per-session sidecar scoping (ROADMAP).
