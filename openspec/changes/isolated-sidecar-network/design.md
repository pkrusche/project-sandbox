# Design

## Context

See `proposal.md` (Why). The current state that shapes the approach:

- `egress_sidecar.Strategy` (`egress_sidecar.py:272`) already picks a per-runtime topology. Colima uses `DOCKER_PRIVATE_NETWORK`: `psbx-egress-uplink` plus an `--internal` `psbx-sandbox` (`colima_network.py`). Every other runtime publishes Squid on one host address and maps `host.docker.internal` into the sandbox.
- **Containment was measured** (`spikes.md`, S1–S6; macOS 27.2.1, Colima v0.10.3 on VZ with Docker 29.5.2, Apple `container` 1.7.1):
  - **Colima, S2.** A sandbox on `psbx-sandbox` with no capabilities and no firewall has no route to the Mac (`192.168.5.2`), the Internet, IPv6, Lima's resolver, the default bridge or `169.254.169.254`. The embedded resolver answers only SERVFAIL. A `NET_ADMIN` sandbox routing through the sidecar gets nowhere (`ip_forward=0`).
    - **One gap:** the plain internal bridge carries the VM's address (`172.19.0.1`), so the VM's sshd on `0.0.0.0:22` answers. Any port another Colima container publishes on `0.0.0.0` would answer too.
    - With `com.docker.network.bridge.gateway_mode_ipv4=isolated` the VM has no IPv4 address on the bridge, the sandbox has IPv6 disabled, and the VM's link-local address is unreachable. The sidecar is still reachable by address and as `egress`.
  - **Colima, S1.** The sidecar reaches Mac services bound to `127.0.0.1` and `0.0.0.0` through `host.lima.internal` (`192.168.5.2`, resolved in containers too). Squid accel ports with `originserver` peers stream NDJSON and SSE incrementally (0.02 s skew), with no tuning. The Mac sees every such request as coming from `127.0.0.1`.
  - **Apple `container`, S3–S5.** `network create --internal` (with labels) works without `sudo` and blocks the Internet. The network is host-only, though: a sandbox on it reaches every Mac service bound to all interfaces (AirPlay on 5000/7000, a test service on `0.0.0.0`), and every Mac loopback service through `203.0.113.113`. The packet-filter redirect behind `host.docker.internal` applies to the host-only network too. Also, `container run` has no `--add-host`, and a container's address changes on every stop/start. **Apple is out of scope** (Non-Goals).
  - **S6.** Concurrent sandboxes show up in Squid's log under their own internal addresses on both runtimes.
- **The sandbox firewall is baked into the image** (`templates/init-firewall.sh.j2`) and run once through a sudo rule from `entrypoint.sh.j2`. The run-once guard and sudo revocation live in the helper. `mount-session-firewall` on `v0.5.x` would make it a runtime mount, but this change is based on `main` and doesn't depend on it.
- **Squid** (`templates/egress-sidecar/squid.conf.j2`):
  - one `http_port` (3128) with `ssl-bump`;
  - a `private_dst` denial from `squid_config.PRIVATE_DESTINATIONS`, which lacks `203.0.113.0/24` (S5);
  - with an upstream, a `cache_peer … default` plus `never_direct allow all`.
- **Sidecar state** has `state.json` (`STATE_VERSION = 2`), drift detection and live `reconfigure`. Readiness is checked with `exec` into the sidecar.
- **The egress-sidecar-macos decision (archived, design.md:61) rejected internal networks.** The reason was that they need "a forwarder per local service inside the sidecar, and on Linux a host-side helper per runtime". This design avoids both: services are Squid routes, not forwarders, and Linux runtimes are out of scope.

## Goals / Non-Goals

**Goals:**
- On Colima, the container network is the containment boundary, and the sandbox firewall is optional defense in depth.
- No new long-running process: Squid is the only listener on the internal network.
- Turning `--sandbox-firewall` on or off, or adding services, never rebuilds an image.
- Every new behaviour is backed by a recorded spike (tasks group 0).

**Non-Goals:**
- **Apple `container`.** It keeps the published-endpoint strategy and its sandbox firewall. Its host-only network can't keep a sandbox away from the Mac (S3). An isolated topology there needs either host packet-filter rules for the host-only network (administrator rights, which the project never uses) or a future `container` network mode with no host path. Both are ROADMAP items.
- **Linux runtimes and Docker Desktop/Podman machine.** They keep the published-endpoint strategy. A per-session namespace-owner gateway for Linux is a ROADMAP item.
- **Non-HTTP traffic,** and transparent interception of clients that ignore proxy variables.
- **Per-session policy or per-session service scoping.** The policy is one global `egress.toml` per runtime.
- **Per-sandbox denial attribution.** S6 shows it's feasible: concurrent sandboxes have distinct addresses, but sequential ones reuse them, so attribution also needs the session's time window. It stays on the ROADMAP.
- **A loopback upstream parent on Colima** (still rejected), and isolation between sandboxes on the internal network.

## Decisions

### D1. Colima's existing strategy becomes the isolated strategy

`--egress sidecar` keeps one meaning, a global hostname-filtering sidecar, and the runtime picks the topology. Colima's existing `DOCKER_PRIVATE_NETWORK` strategy gains:
- service routes;
- the opt-in sandbox firewall;
- an internal network with no VM address (D2).

No other runtime changes strategy. `colima_network.py` stays Colima-only; no per-runtime backend is needed.

*Alternative:* a separate `--egress gateway` mode. Rejected: it doubles the test matrix and the documentation. Also, on Colima sidecar mode already *is* this topology.

### D2. The internal network gives the VM no address

`psbx-sandbox` is created with `--internal -o com.docker.network.bridge.gateway_mode_ipv4=isolated`. Docker then assigns no address to the bridge in the VM. The VM's own listeners are unreachable, and so is any port another container publishes on the VM's interfaces (S2). The uplink network is unchanged.

- **Validation:** `colima_network.validate` requires the option on `psbx-sandbox`, alongside the ownership, role, driver and `Internal` checks.
- **Engine support:** the option needs Docker Engine 28 or later. When the engine rejects it, network creation fails naming the option, the required engine version and the Colima upgrade.
- **Migration:** a `psbx-sandbox` created by an older version passes every check except the option. `up`, `restart` and a sidecar-mode launch replace it (`network rm`, then create) when no container is attached. Otherwise they fail before any container work. The error names the attached sandboxes and says to run `project-sandbox-egress restart` once they have exited. `restart` stops the sidecar before replacing the network.
- **Addresses:** with no address reserved for the bridge, the first container on the network (usually the sidecar) takes the subnet's `.1`. Nothing depends on the sidecar's address: Squid finds it at start, and launches pin it.

Regardless of the network, Squid binds every listener (3128 and the service ports) **only to its address on `psbx-sandbox`**, so a peer on the uplink can't use it. The address is found inside the sidecar at start, because it changes when the sidecar is recreated.

*Alternatives:*
- **`com.docker.network.bridge.inhibit_ipv4=true`.** It passed S2 as well, but it's a general bridge option meant for bridges managed outside Docker. The gateway mode is the documented option for this purpose. It's kept as a fallback only if an engine turns out not to support the gateway mode.
- **Firewall rules in the VM.** Rejected: they'd need a privileged helper per profile, and Colima would recreate the VM state behind them.

### D3. Services are Squid reverse-proxy ports

Each `[egress.services]` entry renders as:
- `http_port <internal-ip>:<port> accel defaultsite=<name>.service no-vhost name=svc_<name>`;
- `cache_peer <host-address> parent <port> 0 no-query no-digest originserver name=svc_<name>`;
- `cache_peer_access svc_<name> allow svc_port_<name>` (a `myportname` ACL), and deny for everything else.

The rule ordering keeps the two paths apart:
1. Requests on a service port are allowed only to their own peer, with `never_direct` for service ports.
2. `CONNECT` and absolute-URI requests on service ports are denied.
3. Service traffic is not cached: `cache deny` on service ports.
4. The rest of the existing rules apply only to the forward port (`myportname fwd`), so `private_dst` still denies host addresses there.

With an upstream configured, the upstream `cache_peer_access` denies service ports, so `never_direct allow all` can't send service traffic to the parent.

S1 confirmed this layout on Colima, and S5 confirmed it on Apple:
- an absolute-URI `GET http://example.com/` on a service port was rewritten to the service and never reached example.com;
- `CONNECT` there got `405`;
- undeclared ports were refused;
- streams arrived incrementally with Squid's defaults.

*Alternatives:*
- **TCP forwarders (`socat` or a Python forwarder) in the sidecar.** These are what sank the squid-sidecar line: a second process, a second config, no HTTP logging, and nothing gained for HTTP-only services.
- **Routing services through the forward proxy with `NO_PROXY` removed.** This needs every client to be proxy-aware, and it collides with the `private_dst` denial.

### D4. The host address is recorded as a restart input

The sidecar reaches the Mac's loopback services at `host.lima.internal`. S1 found it resolves to `192.168.5.2` both in the VM and in containers.

`up` resolves the address inside the sidecar and writes it into `cache_peer` lines as a literal. It is also recorded in `state.json` as a restart input, the same pattern as the Docker Engine gateway, so a change shows up as drift in `status`. `doctor` and `status` probe each declared service from inside the sidecar, so a service whose host port doesn't answer shows as one named failure.

The Mac sees every relayed request as coming from `127.0.0.1` (S1). A host service therefore can't tell sandboxes, or other VM clients, apart, and must rely on its own authentication.

### D5. The sandbox firewall: one baked template, a runtime switch

On `main` the firewall is part of the image, so the Colima template is baked with both behaviours, and the session decides at start:
- **On:** read `egress` from `/etc/hosts` (pinned at launch) and the service ports from a root-owned file in the session inputs. Then install default-deny with only those address/port pairs, no DNS, and IPv6 fail-closed as today.
- **Off:** the entrypoint still invokes the helper through its one-shot sudo rule, and the helper reads `off` from the same session input file. It takes the run-once guard, revokes the sudo rule and installs nothing.

The decision is written by the host into the per-session inputs, never taken from the environment or arguments, so the session command can't influence it. It can't run the helper again anyway.

`container_cli.py` adds `NET_ADMIN`/`NET_RAW` only when the firewall is on. Service changes alter only the session input, so `--no-build` launches pick them up. `mount-session-firewall` later replaces the baked template with a mount, and this design keeps the same inputs so that the port is mechanical.

*Alternative:* skip the helper entirely when the firewall is off. Rejected: the sudo rule would stay usable for the life of the session.

### D6. The firewall default is a code constant gated by evidence

A table in `egress_sidecar` lists the runtimes with verified containment. It starts empty, so Colima's firewall defaults to on. Colima is added only once:
- the D2 network has shipped;
- the end-to-end containment checks (task 7.1, including the VM-listener probe) pass on it.

The same checks run on every run of the suite. Apple `container` is never in the table, because it doesn't use this strategy.

`--sandbox-firewall` and `--no-sandbox-firewall` are mutually exclusive. The second is refused while Colima is unverified.

### D7. Pinning `egress`

`--add-host egress:<ip>` is used as today, with the sidecar's current address on `psbx-sandbox`, and the sandbox's resolver config gets no nameserver it could use.

### D8. Private destinations include Apple's localhost alias range

`squid_config.PRIVATE_DESTINATIONS` gains `203.0.113.0/24`, on every runtime. S5 showed that a forward request for `host.docker.internal` (which resolves to `203.0.113.113`) reached the Mac's loopback service whenever the alias was configured.

In the shipped policy, the hostname allowlist and the IP-literal denial already stop direct requests. The remaining path is an allowlisted name that resolves into that range. Colima's own host address, `192.168.5.2`, is already covered by `192.168.0.0/16`.

## Risks / Trade-offs

- **[Squid becomes the only barrier for HTTP egress]** → The network still contains non-HTTP traffic, and policy correctness matters more. `sidecar-policy-hardening` (on `v0.5.x`) is the follow-up, and the resource limits and non-root Squid stay.
- **[A `psbx-sandbox` without the gateway mode exposes the VM]** → Validation refuses it, and it's replaced as soon as nothing is attached. The firewall stays on by default until Colima is verified (D6).
- **[Docker Engine older than 28 in the VM]** → Network creation fails with the option and the version named. The `inhibit_ipv4` fallback (D2) is kept in reserve.
- **[Every sandbox reaches every declared service]**
  - Local Ollama has no authentication, so any sidecar sandbox on the profile can use it.
  - The agent gateway relies on its own listener authentication.
  - The Mac sees all of these requests as coming from `127.0.0.1` (D4).
  - → Documented; firewall mode is the per-session alternative.
- **[Sandboxes on the internal network reach one another]**
  - The opt-in firewall limits a sandbox's own outbound traffic, but not inbound traffic from a peer.
  - → Documented in the containment matrix, as already on Colima.
- **[Containment depends on Docker networking that can change across releases]** → The containment probes in the end-to-end suite, and the firewall default tied to verified runtimes (D6).

## Migration Plan

1. **Spikes (group 0):** done. Verdicts are in `spikes.md`; Apple `container` was dropped on S3's verdict.
2. **Colima:**
   - the D2 network and its migration first;
   - then services and the firewall switch, behind the existing sidecar mode, with the firewall on by default;
   - Colima joins the D6 table after task 7.1 passes.
3. **Rollback:**
   - install the previous version;
   - run `project-sandbox-egress down` to remove the sidecar;
   - the replaced `psbx-sandbox` still passes the older validation (owner, role, driver, `Internal`), so the older version reuses it;
   - `[egress.services]` is an unknown key for older versions and must be removed from `egress.toml`.

## Open Questions

- Should a service declaration later carry a path prefix or a TLS origin (for example an HTTPS MCP proxy)? The spec currently covers plain-HTTP loopback services only. Adding fields is additive.
- Whether a loopback upstream parent on Colima can reuse the D4 host address. It stays rejected in this change.
