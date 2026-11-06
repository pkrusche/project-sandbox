# Proposal

## Why

`--egress sidecar` starts a new Squid container, a new internal network and a new CA for every sandbox. Running sandboxes in parallel therefore costs one extra VM per sandbox on Apple `container`, and on Docker it exhausts the default network address pool after roughly 25 sessions. Each launch also builds its own policy from flags, so two sandboxes can run under different egress rules without anyone deciding that.

One global sidecar per runtime, configured in one place, fixes both problems. It also gives users a single control point for egress: allow a host once, and every sidecar sandbox gets it immediately.

This change covers the macOS runtimes: Apple `container`, Docker Desktop and Podman machine. On all three the sidecar reaches host services through a runtime-provided `host.docker.internal`. Local-service forwarding on Linux hosts (Docker Engine, rootless Podman with pasta) follows in `global-egress-sidecar-linux`.

## What Changes

- **One global sidecar per runtime.** A single, long-lived Squid sidecar (`project-sandbox-egress`) on a single internal network (`sbx-egress`) serves every `--egress sidecar` sandbox of that runtime. A sidecar-mode launch starts the sidecar if it isn't running and reuses it otherwise. There is no use tracking: the sidecar runs until the user stops it.
- **The global policy is the only policy.** `$XDG_CONFIG_HOME/project-sandbox/egress.toml` holds:
  - the allowlist and the Copilot profile;
  - the upstream parent proxy and its CA;
  - the local-service ports to forward;
  - the CA lifetime;
  - the sidecar's resource limits.

  Every known agent provider host is always allowed. Users who want tighter isolation per sandbox use firewall mode.
- **New `project-sandbox-egress` executable:** `up`, `down`, `restart`, `status`, `reload`, `allow list|add|remove`, `forward list|add|remove`, `log`.
  - Allowlist edits apply to the running sidecar with `squid -k reconfigure`, without a restart.
  - Forwarder edits also apply live.
- **BREAKING:** the `--egress-policy` flag is removed.
- **BREAKING:** in sidecar mode, `project-sandbox` rejects `--extra-domain`, `--internet-proxy` and `--ca-cert`, naming the `project-sandbox-egress` command or `egress.toml` key to use instead. Firewall mode is unchanged.
- **The sidecar owns all helpers.** Squid, every configured local-service forwarder and, on Apple with a remote upstream, the host relay all start with the sidecar, whether or not the launching sandbox needs them. Each sandbox's firewall opens only the local-service ports its own session uses. A launch that needs a port the sidecar doesn't forward fails with the command that adds it.
- **The Apple relay becomes a detached host process** owned by the sidecar rather than by a sandbox's CLI process. Launches and `status` respawn it if it died. A relay port that is no longer free is replaced by reloading Squid.
- **The CA belongs to the sidecar, not the session.** Its lifetime is configurable (default 7 days, maximum 30) and it is reused across restarts while still valid. When interception is active, a launch warns if the CA expires within 2 days and refuses to start if it has expired.
- **Logs and summaries:**
  - The access log records the client IP.
  - At session end the CLI prints how many requests the sidecar denied during the session's time window, across all sandboxes.
  - Attributing denials to individual sandboxes is deferred to ROADMAP.
- **The sidecar runs with memory and CPU limits.** Defaults are configurable in `egress.toml`. On Docker and Podman its runtime log is size-capped.
- **Docs** present the three egress options: firewall, sidecar, or your own filtering proxy through `--internet-proxy`.

## Capabilities

### New Capabilities

- `egress-sidecar-management`: the `project-sandbox-egress` executable and its commands. Covers bringing the global sidecar up and down, restarting it, status, live reload, allowlist and forwarder editing, and log access.

### Modified Capabilities

- `egress-sidecar`:
  - Topology and lifecycle move from per-session to one global, on-demand sidecar per runtime.
  - Policy comes only from `egress.toml`, with all known providers allowed. The CA belongs to the sidecar and has a configurable lifetime.
  - The upstream proxy and Apple relay become sidecar configuration, and forwarders are global with per-session firewall wiring.
  - The Copilot profile is enabled only in the policy file. The session-end summary becomes a denial count.
- `internet-proxy-support`: in sidecar mode, `--internet-proxy` (and with it `--extra-domain` and `--egress-policy`) is no longer accepted at launch. The upstream parent is configured in `egress.toml`.

## Impact

- **Code:**
  - `egress_sidecar.py` is rewritten from a per-session context manager into ensure, up, down and reload operations.
  - `session_ca.py` gains a sidecar-scoped CA with a configurable lifetime.
  - `egress_policy.py` gets the new `egress.toml` keys: upstream, forwarders, CA and resources.
  - `squid_config.py` adds `%>a` to the log format.
  - `internet_proxy.py` / `build_proxy_forwarder.py` provide a detachable relay entry point.
  - `egress_log.py` changes to counting denials in a time window.
  - In `cli.py`: launch validation, removal of `--egress-policy`, ensure-sidecar before the sandbox runs, no sidecar teardown, and dry-run output.
  - New: the `project-sandbox-egress` module and `pyproject.toml` script entry.
- **State on the host:** `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/` holds the rendered config, the CA (key mode 0600), the relay state and per-session sandbox inputs. The CA key now lives as long as the sidecar, up to 30 days, instead of one session.
- **Security:**
  - All sidecar-mode sandboxes share one internal network, so isolation between them rests on the sandbox firewall and Squid's IP-literal and private-address denials.
  - The loopback relay to a corporate proxy persists while the sidecar is up.
  - `docs/security.md` and `docs/egress-sidecar.md` change accordingly.
- **Migration:** resources left by per-session sidecars (label `project-sandbox.egress.session`) are removed by `project-sandbox-egress up`/`down` and by sidecar launches.
- **Tests:** unit tests for the new argv construction, ensure and reload decisions, config parsing, relay supervision, and CLI rejection. The `egress-sidecar` e2e suite gains shared-sidecar scenarios.
- **Out of scope:**
  - Linux local-service forwarding (Docker Engine bridge, rootless Podman with pasta), which is the next change.
  - Pinning the sidecar's IP, so that sandboxes survive a restart.
  - Per-sandbox denial attribution.
  - Upstream proxy authentication.
