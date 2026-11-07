# Proposal

## Why

Firewall mode pins allowed domains to IP addresses once per session. The policy is coarse (`--allow-github` admits all of GitHub) and it breaks when DNS rotates. `--internet-proxy` filters by hostname, but leaves the proxy's lifecycle and policy to the user. This change adds a managed alternative: one Squid sidecar per runtime that filters by hostname and TLS SNI, configured in one place, and shared by every sidecar-mode sandbox.

An earlier implementation (the unreleased `squid-sidecar` line: `add-squid-egress-sidecar`, then `global-egress-sidecar-macos`) put sandboxes on an internal network behind the sidecar. That needed a forwarder inside the sidecar for every local service and, on Linux, extra host processes in other network namespaces. This change keeps the policy and management design that line validated. It drops the internal network: the sidecar is treated like the `internet-proxy-locally` setup that firewall mode already supports, a proxy on the host that the sandbox reaches through the existing local-service forwarding.

This change covers the macOS runtimes: Apple `container`, Docker Desktop and Podman machine. Docker Engine and rootless Podman on Linux follow in `egress-sidecar-linux`.

## What Changes

- **New opt-in mode `--egress sidecar`.** The default (`firewall`) is unchanged.
- **One global sidecar per runtime.** A single, long-lived Squid container, `project-sandbox-egress`, runs on the runtime's default network and publishes its proxy port on host loopback (`127.0.0.1:3129` by default). A sidecar-mode launch starts it if it isn't running and reuses it otherwise. There is no use tracking: the sidecar runs until the user stops it.
- **A sidecar-mode sandbox is a firewall-mode sandbox with an Internet proxy.** It runs on the default network. Its firewall drops everything except the sidecar's endpoint and the local services this session uses, and admits no DNS. Its proxy variables name the sidecar. Local services (agent proxy, Ollama) and the sidecar endpoint itself are reached through the existing per-runtime local-service forwarding. A remote `--agent-proxy` is admitted as in firewall mode.
- **Policy:**
  - Destinations are filtered by hostname, with wildcards, re-resolved on every request.
  - The sidecar verifies that the TLS SNI equals the CONNECT host, and denies IP literals, non-443 CONNECTs, raw tunnels and (in direct mode) private and metadata addresses before any upstream connection.
  - GitHub repository access is blocked. A Copilot profile admits the Copilot CLI by intercepting only `api.github.com` with a path allowlist, under a name-constrained CA.
- **The global policy is the only policy.** `$XDG_CONFIG_HOME/project-sandbox/egress.toml` holds:
  - the allowlist and the Copilot profile;
  - the upstream parent proxy and its CA;
  - the CA lifetime;
  - the sidecar's port and resource limits.

  Every known agent provider host is always allowed.
- **Upstream parent proxy.** The sidecar can chain to one parent proxy, never going direct. On Apple `container`, a remote parent is reached through a detached host relay that the sidecar owns.
- **New `project-sandbox-egress` executable:** `up`, `down`, `restart`, `status`, `reload`, `allow list|add|remove`, `log`. Allowlist edits apply to the running sidecar with `squid -k reconfigure`, without a restart.
- **The CA belongs to the sidecar.** Its lifetime is configurable (default 7 days, maximum 30), and it is reused across restarts while still valid. When interception is active, a launch warns if the CA expires within 2 days and refuses to start if it has expired.
- **Observability.** The access log labels each denial's cause. At session end the CLI prints how many requests the sidecar denied during the session, across all sandboxes, and the `log` command that lists them. Denials are not attributed to individual sandboxes.
- **Rejected in sidecar mode,** each naming the replacement: `--no-firewall`, `--runtime chroot`, `--extra-domain`, `--internet-proxy`, `--ca-cert`, `--allow-github`, `--copilot-host`. Devcontainers stay firewall-only.
- **Docs** present the three egress options (firewall, sidecar, your own proxy through `--internet-proxy`) and the sidecar's security boundary.

## Capabilities

### New Capabilities

- `egress-sidecar`: the `--egress sidecar` mode. Covers the published sidecar endpoint, sandbox confinement, hostname and SNI filtering, minimal TLS interception with the sidecar CA, the GitHub block and Copilot profile, the host-side policy file, the upstream parent and Apple relay, the global lifecycle, drift handling, observability, resource limits and e2e verification.
- `egress-sidecar-management`: the `project-sandbox-egress` executable: bringing the sidecar up and down, restarting it, status, live reload, allowlist editing and log access.

### Modified Capabilities

- `internet-proxy-support`: in sidecar mode, `--internet-proxy` is rejected at launch. The upstream parent is configured in `egress.toml`, where the sidecar enforces local hostname policy in front of it.

## Impact

- **Code (new):**
  - `egress_policy.py`: `egress.toml` model, validation and comment-preserving edits (new pinned dependency `tomlkit`).
  - `squid_config.py`, `sidecar_image.py` and `templates/egress-sidecar/`: the Squid config, image and helpers (`sni-match`, `sbx-reload`).
  - `session_ca.py`: the sidecar CA (new pinned dependency `cryptography`).
  - `egress_sidecar.py`: ensure, down, restart, reload and status for the global sidecar.
  - `egress_relay.py`: the detached Apple relay.
  - `egress_log.py`: log parsing and the denial count.
  - `egress_cli.py`: the `project-sandbox-egress` executable.
- **Code (changed):**
  - `cli.py`: `--egress`, launch validation, ensure-sidecar before the sandbox, dry-run output.
  - `firewall.py` and `init-firewall.sh.j2`: the sidecar endpoint as an Internet-proxy destination, plus runtime installation of the sidecar and upstream CAs.
  - `internet_proxy.py`: the sidecar as a proxy configuration.
  - `doctor.py`: a sidecar status line.
  - `scripts/update-pins.py`: the sidecar image pins.
- **Host state:** `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/` holds the rendered config, the CA (key mode 0600, at most 30 days), relay state and per-session CA mounts.
- **Security:**
  - Sidecar sandboxes have a route to the network like firewall-mode sandboxes; the sandbox firewall is the boundary that keeps them on the sidecar.
  - The sidecar's port is usable by any process on the host, and from any container that can reach host loopback.
  - `docs/security.md` documents both, and the CA key handling.
- **Tests:** unit tests for policy parsing and edits, rendered config, argv construction, ensure and reload decisions, relay supervision, CLI rejections, and the management CLI. Opt-in e2e suites `egress-sidecar` and (billable) `copilot-egress`.
- **Out of scope:**
  - Docker Engine and rootless Podman on Linux (`egress-sidecar-linux`).
  - Per-sandbox denial attribution.
  - Upstream proxy authentication.
  - Per-repository GitHub access (ROADMAP).
  - Sidecar mode for devcontainers and chroot.
