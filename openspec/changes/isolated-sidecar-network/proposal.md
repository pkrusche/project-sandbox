# Proposal

## Why

Sidecar mode currently relies on the sandbox's own iptables for containment. The sandbox sits on a network with a route out, holds `NET_ADMIN`/`NET_RAW` and a one-shot sudo rule for its firewall, and reaches host services through per-runtime workarounds:

- Apple's `203.0.113.113` localhost alias, which can go stale;
- pinning `/etc/hosts` before DNS closes;
- on Colima, refusing Ollama and loopback agent proxies outright.

Colima already shows the alternative. Its sidecar sandboxes sit on an `--internal` network, and the archived Spike B found that a sandbox with no capabilities and no project firewall had no way out. Apple `container` on macOS 26 adds custom networks, so the same topology may now work there.

This change makes the container network the containment boundary for sidecar mode on Colima and Apple `container`. The in-sandbox firewall becomes optional, and the sidecar becomes the single point through which a sandbox reaches the Internet or a host service.

## What Changes

- **Isolated sidecar strategy on Colima and Apple `container`.**
  - Sandboxes join only a project-owned internal network with no route out.
  - The sidecar is attached to that network and to an outbound one, and doesn't route between them.
  - Colima keeps its two existing networks. Apple `container` gets the equivalent pair: a host-only network (`container network create --internal`) and the default NAT network.
  - Docker Desktop, Podman machine, Docker Engine and rootless Podman keep their current published-endpoint strategy.
- **The sidecar is the only listener a sandbox can reach.**
  - Squid serves the forward proxy on `egress:3128`.
  - Each declared host-loopback HTTP service is reverse-proxied on `egress:<port>`.
  - The sandbox is given `http://egress:3128` as its proxy and `http://egress:<port>` for Ollama and a loopback agent proxy.
  - Squid's rules keep the reverse-proxy routes, which deliberately reach non-global host addresses (`192.168.5.2` on Colima, the `host.docker.internal` alias on Apple), separate from forward-proxy traffic. Forward-proxy traffic keeps its private-destination denial.
- **Local services are global policy.**
  - A new `[egress.services]` table in `egress.toml` names each host-loopback HTTP service and its port.
  - `project-sandbox-egress service add|remove|list` edits it, and changes apply to the running sidecar like allowlist edits.
  - On the isolated strategy, a sidecar-mode launch with `--pi-ollama` or a loopback `--agent-proxy` fails before any container work unless a matching service is declared, and names the command that declares it.
  - Every sandbox on the runtime can reach every declared service. Sessions that need more isolation use firewall mode.
- **The in-sandbox firewall is opt-in on the isolated strategy.**
  - By default the sandbox runs without `NET_ADMIN`, `NET_RAW` or the firewall step. The entrypoint still burns the one-shot sudo rule.
  - `--sandbox-firewall` restores a default-deny firewall that admits only the sidecar's address on the proxy and service ports, as defense in depth.
  - The firewall is off by default on a runtime only once that runtime's containment spike has passed. Until then, that runtime defaults to on.
- **Colima gains host services in sidecar mode.** Local Ollama and a loopback `--agent-proxy` are supported through the sidecar. They stay unsupported in Colima firewall mode, and a loopback upstream parent proxy stays unsupported.
- **BREAKING:** sidecar mode on Apple `container` requires macOS 26 or later and the latest supported `container` release. It is refused before any container work on older systems. No sidecar users run macOS 15.
- **Documentation:** a containment matrix in `docs/egress-sidecar.md` showing, per runtime, whether containment is network-level or firewall-level and whether the sandbox firewall is on by default.
- **ROADMAP:** a per-session namespace-owner gateway for Docker and Podman on Linux. A privileged gateway container holds the firewall, and the sandbox joins its network namespace with no capabilities. This is not possible on Apple `container`.

## Capabilities

### New Capabilities

- `sidecar-network-isolation`: the isolated sidecar topology on Colima and Apple `container`, meaning which networks the sandbox and the sidecar join, what a sandbox can and can't reach, the opt-in sandbox firewall and its per-runtime default, runtime minimums, and the containment matrix.
- `sidecar-local-services`: host-loopback HTTP services exposed to sandboxes by the sidecar. This covers the `[egress.services]` policy, reverse-proxy routing to the runtime's host address, separation from forward-proxy rules, launch-time matching for Ollama and agent proxies, and streaming behaviour.

### Modified Capabilities

- `egress-sidecar`:
  - "Sidecar egress is an explicit opt-in mode": Apple `container` requires macOS 26+ and a minimum `container` version.
  - "The sidecar's proxy endpoint is published on the host": Apple `container`, like Colima, publishes no port.
  - "The sandbox reaches the Internet only through the sidecar": limited to the published-endpoint strategy.
  - "Local services reach sidecar-mode sandboxes as in firewall mode": the isolated strategy routes them through the sidecar instead.
  - "The Colima sandbox admits only the sidecar and uses no DNS": the firewall part becomes opt-in, and the pinned name and no-DNS parts stay.
- `egress-sidecar-management`: adds `service add|remove|list`; `status` reports declared services.
- `colima-runtime`: "Host-dependent features are unsupported on Colima" now allows local Ollama and a loopback agent proxy in sidecar mode.

## Impact

- **Code:**
  - `egress_sidecar.py` (an isolated strategy for Apple, the sandbox plan without host aliases, capabilities and the firewall flag, readiness without a published port);
  - `colima_network.py`, generalized or given a sibling for Apple networks;
  - `egress_policy.py` (`[egress.services]`);
  - `squid_config.py` and `templates/egress-sidecar/squid.conf.j2` (accel ports, `cache_peer` origin servers, separate ACLs);
  - `egress_cli.py` (`service`);
  - `cli.py` (`--sandbox-firewall`, the macOS and `container` version check, service matching);
  - `container_cli.py` (no capabilities when the firewall is off);
  - `templates/entrypoint.sh.j2` (skip the firewall but revoke sudo);
  - `templates/init-firewall.sh.j2` (isolated-strategy policy read from session inputs);
  - `doctor.py`.
- **Runtimes:** Apple `container` sidecar mode needs macOS 26+. Colima networks are reused unchanged. Other runtimes are unaffected.
- **Security:**
  - Containment moves from a guest-kernel firewall to the runtime's network isolation (vmnet host-only, a Docker internal bridge).
  - Every sandbox on a runtime can reach every declared service.
  - Sandboxes on the internal network can reach one another, as on Colima today.
  - These properties change and must be documented in `docs/security.md`.
- **Related, on `v0.5.x`, not depended on:**
  - `mount-session-firewall` would let the opt-in firewall be a runtime mount, with no strategy-specific image.
  - `sidecar-policy-hardening` applies to the same Squid config. Its importance grows because the sidecar becomes the only barrier.
  - `scope-firewall-allowlist`'s per-agent provider table could later feed the sidecar allowlist.
  - All three are reconciled when they are brought onto `main`.
- **Process:** implementation is gated on spikes S1–S6 (tasks group 0). A failed blocking spike stops the change for that runtime.
