# Design

## Context

See `proposal.md` (Why). The current state that shapes the approach:

- `egress_sidecar.Strategy` (`egress_sidecar.py:272`) already picks a per-runtime topology. Colima uses `DOCKER_PRIVATE_NETWORK`, with `psbx-egress-uplink` plus an `--internal` `psbx-sandbox` (`colima_network.py`). Every other runtime publishes Squid on one host address and maps `host.docker.internal` into the sandbox.
- **Colima containment is already measured.** The archived Spike B (`openspec/changes/archive/2026-11-15-add-colima-hardened-profile/spikes.md`, section B) ran a container on `psbx-sandbox` with no `NET_ADMIN` and no project firewall. It found no TCP, UDP, DNS or IPv6 route out, no access to the bridge gateway, the uplink address or `169.254.169.254`, and the sidecar wasn't routing (`ip_forward=0`). Not measured: `192.168.5.2` (`host.lima.internal`, the Mac).
- **The sandbox firewall is baked into the image** (`templates/init-firewall.sh.j2`) and run once through a sudo rule from `entrypoint.sh.j2`. The run-once guard and sudo revocation live in the helper. `mount-session-firewall` on `v0.5.x` would make it a runtime mount, but this change is based on `main` and doesn't depend on it.
- **Squid** (`templates/egress-sidecar/squid.conf.j2`):
  - one `http_port` (3128) with `ssl-bump`;
  - a `private_dst` denial;
  - with an upstream, a `cache_peer … default` plus `never_direct allow all`.
- **Sidecar state** has `state.json` (`STATE_VERSION = 2`), drift detection and live `reconfigure`. Readiness is checked with `exec` into the sidecar.
- **The egress-sidecar-macos decision (archived, design.md:61) rejected internal networks.** The reason was that they need "a forwarder per local service inside the sidecar, and on Linux a host-side helper per runtime". This design avoids both: services are Squid routes, not forwarders, and Linux runtimes are out of scope.

## Goals / Non-Goals

**Goals:**
- One isolated strategy, shared by Colima and Apple `container`, that differs only in network creation and in the host address used for services.
- No new long-running process: Squid is the only listener on the internal network.
- Turning `--sandbox-firewall` on or off, or adding services, never rebuilds an image.
- Every new behaviour is decided by spikes before implementation (tasks group 0).

**Non-Goals:**
- **Linux runtimes and Docker Desktop/Podman machine.** They keep the published-endpoint strategy. A per-session namespace-owner gateway for Linux is a ROADMAP item.
- **Non-HTTP traffic,** and transparent interception of clients that ignore proxy variables.
- **Per-session policy or per-session service scoping.** The policy is one global `egress.toml` per runtime.
- **Per-sandbox denial attribution.** S6 records whether source IPs are visible, and the feature itself stays on the ROADMAP.
- **A loopback upstream parent on Colima** (still rejected), and isolation between sandboxes on the internal network.

## Decisions

### D1. A sidecar strategy, not a new egress mode

`--egress sidecar` keeps one meaning, a global hostname-filtering sidecar, and the runtime picks the topology. The existing `DOCKER_PRIVATE_NETWORK` strategy becomes the general isolated strategy, with a per-runtime network backend. `colima_network.py` is generalized (argv builders per runtime, ownership labels, attachment checks) rather than copied.

*Alternative:* a separate `--egress gateway` mode. Rejected: it doubles the test matrix and the documentation. Also, on Colima sidecar mode already *is* this topology.

### D2. Apple networks mirror Colima's: a dedicated uplink and an internal network

On Apple `container`, the sidecar joins two project-owned networks, both created without `sudo`:
- `psbx-egress-uplink`, a NAT network;
- `psbx-sandbox`, a host-only network (`container network create --internal`).

The sidecar doesn't use the `default` network. Firewall-mode sandboxes live on `default`, and a dedicated uplink keeps them away from the sidecar's listeners.

Regardless of the network, Squid binds every listener (3128 and the service ports) **only to its address on `psbx-sandbox`**, so a peer on the uplink can't use it. The address is found inside the sidecar at start, because it changes when the sidecar is recreated.

*Alternative:* use `default` as the uplink. That saves one network, but firewall-mode sandboxes on `default` would share a segment with the sidecar. Rejected.

*Depends on* S3 (internal networks without `sudo`, no route out) and S4 (one container on two networks, container-to-container on a host-only network). If S4 shows that vmnet host-only networks isolate containers from each other, the Apple half of the change stops.

### D3. Services are Squid reverse-proxy ports

Each `[egress.services]` entry renders as:
- `http_port <internal-ip>:<port> accel defaultsite=<name>.service no-vhost name=svc_<name>`;
- `cache_peer <host-address> parent <port> 0 no-query no-digest originserver name=svc_<name>`;
- `cache_peer_access svc_<name> allow svc_port_<name>` (a `myportname` ACL), and deny for everything else.

The rule ordering keeps the two paths apart:
1. Requests on a service port are allowed only to their own peer.
2. `CONNECT` and absolute-URI requests on service ports are denied.
3. Service traffic is not cached: `cache deny` on service ports.
4. The rest of the existing rules apply only to the forward port (`myportname fwd`), so `private_dst` still denies host addresses there.

With an upstream configured, the upstream `cache_peer_access` denies service ports, so `never_direct allow all` can't send service traffic to the parent.

Streaming depends on Squid not buffering accel responses (`read_ahead_gap`, no `delay_pools`, no response adaptation). S1 and S5 measure first-chunk latency for an Ollama stream and an SSE stream.

*Alternatives:*
- **TCP forwarders (`socat` or a Python forwarder) in the sidecar.** These are what sank the squid-sidecar line: a second process, a second config, no HTTP logging, and nothing gained for HTTP-only services.
- **Routing services through the forward proxy with `NO_PROXY` removed.** This needs every client to be proxy-aware, and it collides with the `private_dst` denial.

### D4. Host address per runtime, recorded as a restart input

The sidecar reaches the Mac's loopback services as follows:
- **Colima:** `host.lima.internal`, resolved inside the VM (expected `192.168.5.2`).
- **Apple `container`:** `host.docker.internal` through the administrator-managed localhost alias (`203.0.113.113`, `docs/apple-container-dns.md`). The alias is now needed only by the sidecar, never by a sandbox, and only when a service or a relay is used.

`up` resolves the address inside the sidecar and writes it into `cache_peer` lines as a literal. It is also recorded in `state.json` as a restart input, the same pattern as the Docker Engine gateway, so a change shows up as drift in `status`. `doctor` and `status` probe each declared service from inside the sidecar. A stale Apple alias then shows as one named failure, not as a timeout in every sandbox.

*Depends on* S1 (Colima VM to Mac loopback) and S5 (Apple sidecar to Mac loopback through the alias).

### D5. The sandbox firewall: one baked template, a runtime switch

On `main` the firewall is part of the image, so the isolated-strategy template is baked with both behaviours, and the session decides at start:
- **On:** read `egress` from `/etc/hosts` (pinned at launch) and the service ports from a root-owned file in the session inputs. Then install default-deny with only those address/port pairs, no DNS, and IPv6 fail-closed as today.
- **Off:** the entrypoint still invokes the helper through its one-shot sudo rule, and the helper reads `off` from the same session input file. It takes the run-once guard, revokes the sudo rule and installs nothing.

The decision is written by the host into the per-session inputs, never taken from the environment or arguments, so the session command can't influence it. It can't run the helper again anyway.

`container_cli.py` adds `NET_ADMIN`/`NET_RAW` only when the firewall is on. Service changes alter only the session input, so `--no-build` launches pick them up. `mount-session-firewall` later replaces the baked template with a mount, and this design keeps the same inputs so that the port is mechanical.

*Alternative:* skip the helper entirely when the firewall is off. Rejected: the sudo rule would stay usable for the life of the session.

### D6. Per-runtime firewall default as a code constant gated by evidence

A table in `egress_sidecar` lists the runtimes with verified containment. Both runtimes start out not verified, so the firewall defaults to on. Task 0.7 adds a runtime only after its spike verdicts are PASS, and the end-to-end containment checks (tasks group 7) assert the same probes on every run of the suite.

`--sandbox-firewall` and `--no-sandbox-firewall` are mutually exclusive. The second is refused on unverified runtimes.

### D7. Pinning `egress` on Apple

On Colima, `--add-host egress:<ip>` is used as today. On Apple `container`, S4 checks whether `container run` supports `--add-host`. If not, the entrypoint's root step writes the pin from the session inputs before dropping privileges, using the same input file as D5. Either way, the sandbox's resolver config gets no nameserver it could use.

### D8. Version gate for Apple

A launch on Apple `container` reads `sw_vers -productVersion` and `container --version` before any network, key or container work. The minimum `container` version is the first release that passes S3 and S4, pinned as a constant next to the existing 1.4.1 check and reported by `doctor`.

### D9. Migration of a running Apple sidecar

`STATE_VERSION` becomes 3, and the strategy is recorded in `state.json`. On a machine with a published-endpoint Apple sidecar from an older version, the next `up` or sidecar-mode launch finds a state mismatch. It replaces the container as the existing drift handling does: the published port goes away and the networks are created.

## Risks / Trade-offs

- **[vmnet host-only isolates containers from each other]** → S4 blocks the Apple half. Colima can still ship alone.
- **[Squid buffers streamed responses]** → S1 and S5 measure it. If tuning can't fix it, services need a different relay, and that reopens D3 before any implementation.
- **[Squid becomes the only barrier for HTTP egress]** → The network still contains non-HTTP traffic, and policy correctness matters more. `sidecar-policy-hardening` (on `v0.5.x`) is the follow-up, and the resource limits and non-root Squid stay.
- **[Every sandbox reaches every declared service]**
  - Local Ollama has no authentication, so any sidecar sandbox on the runtime can use it.
  - The agent gateway relies on its own listener authentication.
  - → Documented; firewall mode is the per-session alternative.
- **[Sandboxes on the internal network reach one another]**
  - The opt-in firewall limits a sandbox's own outbound traffic, but not inbound traffic from a peer.
  - → Documented in the containment matrix, as already on Colima.
- **[Containment depends on runtime networking that can change across releases]** → A minimum `container` version, the containment probes in the end-to-end suite, and the firewall default tied to verified runtimes (D6).
- **[The Apple alias goes stale]** → The blast radius shrinks to services and the relay, and the sidecar-side probe in `status`/`doctor` (D4) names it.
- **[`192.168.5.2` is reachable from Colima's default bridge]** → Firewall-mode containers on the default bridge can already reach it, so nothing changes for them. S2 confirms that `psbx-sandbox` can't.

## Migration Plan

1. **Spikes (group 0).** Record verdicts in `spikes.md` in this change. A BLOCKED verdict for a runtime removes that runtime from scope before any code.
2. **Colima first.** It needs no new networks. Services and the firewall switch ship behind the existing sidecar mode.
3. **Apple `container` second,** behind the version gate. The first launch or `up` replaces a published-endpoint sidecar (D9).
4. **Rollback:**
   - install the previous version;
   - run `project-sandbox-egress down` to remove the sidecar;
   - the leftover Apple networks are inert, and can be removed with `container network rm psbx-sandbox psbx-egress-uplink`;
   - `[egress.services]` is an unknown key for older versions and must be removed from `egress.toml`.

## Open Questions

- Should a service declaration later carry a path prefix or a TLS origin (for example an HTTPS MCP proxy)? The spec currently covers plain-HTTP loopback services only. Adding fields is additive.
- Whether a loopback upstream parent on Colima can reuse the D4 host address. It stays rejected in this change.
