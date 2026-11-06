# Design

## Context

This change builds on `global-egress-sidecar-macos`: one global sidecar per runtime, a `sidecar/forwarders` file reconciled inside the sidecar by `sbx-forwarders`, and a detached, sidecar-owned host process (`egress_relay`) supervised through `relay.lock`. On Linux:

- **Docker Engine:** `host-gateway` resolves to the bridge gateway, so a host service bound to `127.0.0.1` is unreachable. Firewall mode bridges this with a host `socat` on the `docker0` gateway (`local_service_network` strategy `linux-bridge-socat`). Every container on `docker0` can reach that listener.
- **Rootless Podman:** containers on netavark networks live in the rootless network namespace, whose uplink is pasta (Podman 5 default) or slirp4netns. The host cannot bind an address inside that namespace. Podman 5.3 added pasta's `--map-guest-addr` mapping, which makes `host.containers.internal` reach the host, including its loopback.
- The existing probes (`_docker_is_desktop`, `_podman_uses_local_pasta`, `_podman_security`) already tell these cases apart.

## Goals / Non-Goals

**Goals:**
- Reach loopback-only host services from the sidecar on Docker Engine and rootless Podman.
- Make the forwarding path no more exposed than on macOS: only the sidecar can use it, and sandboxes still need their own firewall rule (from the macOS change).
- Keep one host process per sidecar.

**Non-Goals:**
- Rootless Docker.
- Rootful Podman (already refused).
- Changing firewall mode's `linux-bridge-socat`.

## Decisions

### 1. Docker Engine: a private uplink network

The sidecar's egress side becomes `sbx-egress-uplink`, a regular (non-internal) user bridge that only `up` attaches the sidecar to. It is labelled like `sbx-egress` and removed by `down` when nothing is attached. On Docker Engine the sidecar run uses `--network sbx-egress-uplink` instead of the default network, then `network connect sbx-egress`, as today.

Why: a listener on the `docker0` gateway, as in firewall mode, is reachable from every container on the default bridge. The gateway of a bridge that only the sidecar joins limits the listener's audience to the sidecar by construction. The peer check in Decision 2 is the second layer.

*Alternative:* bind the host forwarder on the `docker0` gateway with a peer check only. Rejected: it relies on a single check, and the sidecar's address on `docker0` can change on restart.

### 2. Host forwarders in the sidecar-owned host process

`egress_relay` becomes `egress_host_helper`, with two optional components:
- the Apple relay (unchanged);
- TCP forwarders on Docker Engine.

The lock, pid, log and spawn logic from the macOS change is unchanged; only the name and the component configuration grow.

Each forwarder:
1. binds `<uplink gateway>:<port>`;
2. accepts a connection only when the peer address equals the sidecar's uplink IP (recorded in `state.json` and passed to the helper);
3. connects to `127.0.0.1:<port>` and pumps bytes with the existing `build_proxy_forwarder` relay loop.

The helper re-reads `sidecar/forwarders` on SIGHUP. `forward add|remove` and `ensure()` send SIGHUP after reconciling the in-sidecar forwarders. The sidecar runs with `--add-host host.docker.internal:<uplink gateway>`, so its in-sidecar `socat` targets do not change.

*Alternative:* spawn host `socat` processes as firewall mode does. Rejected: it adds a host dependency for sidecar mode, needs its own supervision of several children, and socat's `range=` cannot express "this one IP on this one interface" more clearly than a Python check.

**The uplink gateway must exist before binding.** The helper starts after `network create sbx-egress-uplink`, then reads the gateway with `network inspect`. When the network is recreated with a different gateway, the helper restarts with the new address, because it is tied to the restart fingerprint.

### 3. Rootless Podman with pasta: `host.containers.internal`

On rootless Podman 5.3 or later with pasta, the sidecar's in-sidecar forwarders target `host.containers.internal` (`SBX_FORWARD_HOST`). Pasta's guest-address mapping sends that to host loopback. No host process and no uplink network are needed: the sidecar keeps the default network.

Spike 0.1 confirms that the mapping reaches a `127.0.0.1`-only service from a container attached to both a netavark bridge and an `--internal` network. If it does not, rootless Podman keeps the macOS change's rejection, and this change's spec delta is reduced to Docker Engine before implementation continues.

Version gating uses `podman info`'s version together with `rootlessNetworkCmd`. slirp4netns, and pasta older than 5.3, fail with a message naming both conditions.

### 4. Strategy selection

| Runtime probe | Sidecar egress network | Forward target | Host process component |
|---|---|---|---|
| Apple / Docker Desktop / Podman machine | default | `host.docker.internal` (runtime) | relay only (Apple, remote upstream) |
| Docker Engine (rootful) | `sbx-egress-uplink` | `host.docker.internal` → uplink gateway | TCP forwarders |
| Podman rootless, pasta ≥ 5.3 | default | `host.containers.internal` | none |
| Rootless Docker, Podman slirp4netns, pasta < 5.3 | default | — | — (forwarders rejected) |

Rootless Docker is detected from `docker info` (`SecurityOptions` contains `rootless`).

## Risks / Trade-offs

- **[Host forwarders listen on a non-loopback address]** → The bridge has a single member, the peer check runs on every connection, and only configured ports listen. `docs/security.md` compares this with firewall mode's `docker0` listener.
- **[The pasta mapping varies by Podman build]** → Spike 0.1 and version gating. If the probe fails at runtime, the forwarding check in the readiness step reports it.
- **[The uplink subnet collides with a host network]** → Docker allocates it from its address pools, as for any bridge. `status` shows the subnet.
- **[A host firewall such as ufw blocks the uplink gateway]** → The readiness step probes one forwarded port from inside the sidecar and fails with a message about the host firewall.

## Migration Plan

- No user action is needed. On Docker Engine, the first `up` after upgrading creates `sbx-egress-uplink`. A running sidecar from the macOS change shows restart drift ("network layout"), and `project-sandbox-egress restart` moves it.
- Rollback: `project-sandbox-egress down` removes the uplink network along with everything else.
