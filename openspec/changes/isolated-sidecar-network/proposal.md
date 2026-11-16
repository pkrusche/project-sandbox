# Proposal

## Why

On Colima, sidecar mode puts sandboxes on an `--internal` network, but containment still relies on the sandbox's own iptables:

- the sandbox holds `NET_ADMIN`/`NET_RAW` and a one-shot sudo rule for its firewall;
- local Ollama and loopback agent proxies are refused outright, because the sandbox has no path to the Mac.

The archived Spike B found that a sandbox on that internal network, with no capabilities and no project firewall, had no route out. This change's spikes (`spikes.md`) completed that measurement:

- **S2 (Colima): one gap, with a fix.** A plain `--internal` bridge still gives the VM an address, so such a sandbox reaches the VM's own listeners: sshd on `0.0.0.0:22`, and any port another Colima container publishes on `0.0.0.0`. A bridge created with `com.docker.network.bridge.gateway_mode_ipv4=isolated` gives the VM no address on it. The VM, the Mac, the Internet and the metadata address are then all unreachable, and the sidecar is still reachable.
- **S1 (Colima):** the sidecar can serve host-loopback HTTP services as Squid reverse-proxy ports, and streamed responses arrive incrementally.
- **S3 (Apple `container`): BLOCKED.** Its `--internal` network is host-only. A sandbox on it reaches every Mac service bound to all interfaces, and every Mac loopback service through the `203.0.113.113` `host.docker.internal` alias that the sidecar itself needs. Closing that needs packet-filter rules on the Mac, so Apple `container` is out of scope.

This change makes the container network the containment boundary for sidecar mode on Colima. The in-sandbox firewall becomes optional, and the sidecar becomes the single point through which a sandbox reaches the Internet or a host service.

## What Changes

- **The Colima sandbox network gives the VM no address.**
  - `psbx-sandbox` is created with `-o com.docker.network.bridge.gateway_mode_ipv4=isolated` (Docker Engine 28 or later in the VM).
  - Network validation checks the option.
  - An existing `psbx-sandbox` without it is replaced as soon as nothing is attached to it.
  - The sidecar keeps the two existing networks and doesn't route between them.
  - Apple `container`, Docker Desktop, Podman machine, Docker Engine and rootless Podman keep their current published-endpoint strategy.
- **The sidecar is the only listener a Colima sandbox can reach.**
  - Squid serves the forward proxy on `egress:3128`.
  - Each declared host-loopback HTTP service is reverse-proxied on `egress:<port>`.
  - The sandbox is given `http://egress:3128` as its proxy and `http://egress:<port>` for Ollama and a loopback agent proxy.
  - Squid's rules keep the reverse-proxy routes, which deliberately reach the Mac at `host.lima.internal` (`192.168.5.2`), separate from forward-proxy traffic. Forward-proxy traffic keeps its private-destination denial.
- **Local services are global policy.**
  - A new `[egress.services]` table in `egress.toml` names each host-loopback HTTP service and its port.
  - `project-sandbox-egress service add|remove|list` edits it, and changes apply to the running sidecar like allowlist edits.
  - A Colima sidecar-mode launch with `--pi-ollama` or a loopback `--agent-proxy` fails before any container work unless a matching service is declared, and names the command that declares it.
  - Every sandbox on the profile can reach every declared service. Sessions that need more isolation use firewall mode.
- **The in-sandbox firewall is opt-in on Colima.**
  - By default the sandbox runs without `NET_ADMIN`, `NET_RAW` or the firewall step. The entrypoint still burns the one-shot sudo rule.
  - `--sandbox-firewall` restores a default-deny firewall that admits only the sidecar's address on the proxy and service ports, as defense in depth.
  - The firewall defaults to off only once the isolated-mode network has shipped and the end-to-end containment checks pass. Until then, it defaults to on.
- **Colima gains host services in sidecar mode.** Local Ollama and a loopback `--agent-proxy` are supported through the sidecar. They stay unsupported in Colima firewall mode, and a loopback upstream parent proxy stays unsupported.
- **Squid hardening, every runtime:** `203.0.113.0/24`, the range of Apple `container`'s localhost alias, joins the private destinations the forward proxy denies (S5).
- **Documentation:** a containment matrix in `docs/egress-sidecar.md` showing, per runtime, whether containment is network-level or firewall-level and whether the sandbox firewall is on by default. It also records why Apple `container` stays firewall-level.
- **ROADMAP:**
  - a per-session namespace-owner gateway for Docker and Podman on Linux: a privileged gateway container holds the firewall, and the sandbox joins its network namespace with no capabilities;
  - an isolated sidecar network for Apple `container`, which needs host packet-filter rules or a future `container` network mode without a host path.

## Capabilities

### New Capabilities

- `sidecar-local-services`: host-loopback HTTP services exposed to Colima sidecar-mode sandboxes by the sidecar. This covers the `[egress.services]` policy, reverse-proxy routing to the Mac, separation from forward-proxy rules, launch-time matching for Ollama and agent proxies, and streaming behaviour.

### Modified Capabilities

- `egress-sidecar`:
  - "On Colima the sidecar is reached over a private network": the internal network is created with the isolated gateway mode, validated and migrated.
  - "A Colima sandbox has no route out except the sidecar": also the VM's own listeners, the Mac and the metadata address, without the firewall or capabilities.
  - "The Colima sandbox admits only the sidecar and uses no DNS": the firewall part becomes opt-in, and the pinned name and no-DNS parts stay.
  - "The sandbox reaches the Internet only through the sidecar" and "Local services reach sidecar-mode sandboxes as in firewall mode": Colima is the exception.
  - "Destinations are filtered by hostname with SNI verification": the private destinations include `203.0.113.0/24`.
  - Added: the opt-in Colima sandbox firewall, its verified default, revoking the one-shot privilege, and the containment matrix.
- `egress-sidecar-management`: adds `service add|remove|list`; `status` reports declared services and, on Colima, the internal address and networks.
- `colima-runtime`: "Host-dependent features are unsupported on Colima" now allows local Ollama and a loopback agent proxy in sidecar mode.

## Impact

- **Code:**
  - `colima_network.py`: the gateway-mode option, its validation, and replacing a legacy network;
  - `egress_sidecar.py`: Squid's listeners on the internal address, the host address as a restart input, and the sandbox plan without capabilities or a firewall when off;
  - `egress_policy.py`: `[egress.services]`;
  - `squid_config.py` and `templates/egress-sidecar/squid.conf.j2`: accel ports, `cache_peer` origin servers, separate ACLs, `203.0.113.0/24`;
  - `egress_cli.py`: `service`;
  - `cli.py`: `--sandbox-firewall`, service matching, the relaxed Colima rejection;
  - `container_cli.py`: no capabilities when the firewall is off;
  - `templates/entrypoint.sh.j2`: skip the firewall but revoke sudo;
  - `templates/init-firewall.sh.j2`: the Colima policy read from session inputs;
  - `doctor.py`.
- **Runtimes:**
  - Colima needs Docker Engine 28 or later in the VM; Colima v0.10.3 ships 29.5.2.
  - An existing `psbx-sandbox` is replaced once, when nothing is attached to it.
  - Other runtimes are unaffected, apart from the private-destination hardening.
- **Security:**
  - On Colima, containment moves from a guest-kernel firewall to Docker's internal bridge with no VM address.
  - Every sandbox on a profile can reach every declared service, and the Mac sees those requests as coming from `127.0.0.1`.
  - Sandboxes on the internal network can reach one another, as today.
  - These properties change and must be documented in `docs/security.md`.
- **Related, on `v0.5.x`, not depended on:**
  - `mount-session-firewall` would let the opt-in firewall be a runtime mount, with no mode-specific image.
  - `sidecar-policy-hardening` applies to the same Squid config. Its importance grows because the sidecar becomes the only barrier.
  - `scope-firewall-allowlist`'s per-agent provider table could later feed the sidecar allowlist.
  - All three are reconciled when they are brought onto `main`.
- **Process:** spikes S1–S6 are recorded in `spikes.md` (tasks group 0). Apple `container` was dropped on S3's BLOCKED verdict.
