# Design

## Context

`egress-sidecar-macos` runs one Squid sidecar per runtime. It publishes `127.0.0.1:<port>` (default 3129) and plans each sidecar-mode sandbox as a firewall-mode sandbox whose Internet proxy is that endpoint, forwarded with the runtime's `local_service_network` strategy. On Linux, firewall mode already has two strategies for host-loopback services:

- **Docker Engine, `linux-bridge-socat`:** a host `socat` per session and port, bound on the `docker0` gateway, forwarding to `127.0.0.1:<port>`; the sandbox maps `host.docker.internal` to the gateway. `_validate_bindable` fails when that address and port are already bound, so two concurrent sessions cannot use the same local service.
- **Rootless Podman with pasta, `podman-pasta-forward`:** the container runs with `--network pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1,-4,-T,<port>:<port>…`. pasta forwards exactly the listed ports from the container's loopback to host loopback; `host.docker.internal` maps to `127.0.0.1`. The private `198.18.0.2` address stops pasta from copying the host's addresses into the container, which would hide services on the host's own LAN address.

The probes `_docker_is_desktop`, `_podman_uses_local_pasta` and `_podman_security` tell the runtimes apart.

Earlier Linux spikes of the squid-sidecar line (`jj file show -r vrxzuotu openspec/changes/global-egress-sidecar-linux/design.md`) explored keeping an internal network. Their results that still matter here:

- **Podman 5.4.2, pasta:** `host.containers.internal` (`169.254.1.2`, `--map-guest-addr`) reaches services on the host's non-loopback addresses, not `127.0.0.1`. Only `--map-host-loopback` reaches loopback, and for netavark networks that is an account-wide setting. Podman refuses to combine a pasta network with any other network ("cannot set multiple networks without bridge network mode"; `network connect` on a pasta container: "pasta is not supported"). The rootless network namespace carries a copy of the host's LAN address (`192.168.1.144/24` on the spike host).
- **Docker Engine 29.9.2:** a host listener on a bridge gateway is reachable from containers on every bridge, because the gateway is a host address and traffic to it takes the host's INPUT path, which Docker's inter-bridge isolation does not filter. Source addresses are preserved on that path (`172.17.0.2` from `docker0`, `172.18.0.2` from a user bridge), so a peer check reliably tells containers apart.

## Goals / Non-Goals

**Goals:**
- Sidecar mode on Docker Engine and rootless Podman with pasta, with the macOS change's behaviour: concurrent sandboxes on one sidecar, local services, and parent proxies, including one on host loopback.
- Reuse firewall mode's forwarding for the sandbox side; add code only where the sidecar itself needs host loopback.

**Non-Goals:**
- Rootless Docker (rejected), rootful Podman (already refused).
- Fixing firewall mode's per-session `linux-bridge-socat` bind conflict for local services (TODO.md).
- Making ufw or other host firewalls work automatically.

## Decisions

### 1. Per-runtime endpoint

| Runtime | Sidecar network | Published on | Sandbox reaches the sidecar via | Local services |
|---|---|---|---|---|
| Docker Engine (rootful) | default `bridge` | `<docker0 gateway>:<port>` | `host.docker.internal` → gateway (`--add-host`), no listener | `linux-bridge-socat` |
| Rootless Podman, pasta | pasta, private address `198.18.0.2` | `127.0.0.1:<port>` | pasta `-T,<port>:<port>`, `host.docker.internal` → `127.0.0.1` | pasta `-T` |
| Rootless Podman, slirp4netns | default | `127.0.0.1:<port>` | firewall mode's `podman-native-host-alias` checks and alias | same |
| Rootless Docker | — | — | rejected | — |

- **Docker Engine publishes on the gateway, not loopback.** With a loopback publish the sandbox side would need firewall mode's per-session `socat` for the sidecar port, and the second concurrent sandbox would fail to bind it. Publishing on the gateway lets the runtime's own port forwarding serve every sandbox. The gateway is read with `network inspect bridge` at `up` and recorded in `state.json`; it is a restart input, because sandboxes bake the endpoint.
- **The sandbox plan for the sidecar endpoint on Docker Engine** is a gateway destination without a `socat`: `--add-host host.docker.internal:<gateway>`, and the firewall admits `<gateway>:<port>`. Other local services keep `linux-bridge-socat`, whose listeners bind the same gateway on their own ports.
- **The rootless Podman sidecar runs on pasta,** the same network argument firewall mode uses for builds and sessions, plus `--publish 127.0.0.1:<port>:3128`. This avoids the host-LAN-address shadowing seen in the rootless network namespace, and gives the sidecar `-T` forwarding for a loopback parent (Decision 2).
- **Rootless Docker** is detected from `docker info` (`SecurityOptions` contains `rootless`) and rejected for sidecar mode before any container work. Its network namespace has the same loopback problem and is untested.

*Spike results (2026-11-07, Debian 13, Docker Engine 29.9.2, rootless Podman 5.4.2 with passt 0.0~git20250503):*
- Docker Engine: a container published on `172.17.0.1:13129` answered two concurrent containers that mapped `host.docker.internal` to `172.17.0.1`. It answered the host on the gateway address and refused on `127.0.0.1`.
- Rootless Podman: a container published on `127.0.0.1:13129` answered two concurrent containers running on `pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1,-4,-T,13129:13129` with `host.docker.internal` mapped to `127.0.0.1`. The proxy logged every client as `192.168.1.144` (the host's LAN address), confirming that the sidecar cannot tell sandboxes apart.
- Rootless Podman, sidecar on pasta: the same container on `pasta:…,-T,13140:13140` with `--publish 127.0.0.1:13129:3128` reached a `127.0.0.1`-only service on 13140 and the Internet, and still served a pasta sandbox on 13129. That sandbox, with only 13129 forwarded, was refused on 13140.

### 2. A parent proxy on host loopback

`squid_config.upstream_for()` maps a loopback parent to `host.docker.internal:<port>`. The sidecar must therefore reach host loopback:

- **Rootless Podman:** add `-T,<parent port>:<parent port>` to the sidecar's pasta network and map `host.docker.internal` to `127.0.0.1` (spike above). The parent port is a restart input. Sandboxes do not get this port, so they cannot bypass the sidecar to the parent.
- **Docker Engine: a sidecar-owned parent forwarder.** `egress_relay` gains a TCP-forward mode alongside the Apple CONNECT relay. Same process model: detached, `relay.lock`, `relay.pid`, `relay.port`, `relay.log`, respawned by launches, stopped by `down`.
  1. It binds `<docker0 gateway>:<free port>` (port 0, recorded as `relay.port`). This avoids colliding with a firewall-mode `socat` that forwards the same parent port for another session.
  2. It accepts a connection only when the peer address equals the sidecar's `docker0` address, read from `inspect` after `run` and recorded in `state.json`. Any other peer is closed without connecting upstream.
  3. It connects to `127.0.0.1:<parent port>` and pumps bytes with the existing relay loop.

  The sidecar runs with `--add-host host.docker.internal:<gateway>`, and `squid.conf` names `host.docker.internal:<relay.port>` as the parent. The forwarder starts after `run` (it needs the sidecar's address) and before readiness. Squid connects to the parent lazily, so the order is safe. A cold start always restarts the forwarder, because the sidecar's address may change. A forwarder respawned on a different port re-renders and reloads `squid.conf`, as the Apple relay does.
- *Alternative:* a host `socat` with `range=<sidecar IP>/32`. Rejected: it adds a host dependency to sidecar mode and a second supervision scheme.
- *Alternative:* run the sidecar with host networking on Docker Engine. Rejected: Squid would listen on every host interface, and the published-port model would differ per runtime.

### 3. Host-side checks

- `up` checks that the publish address and port are free: the gateway on Docker Engine, loopback otherwise. It names `[egress.sidecar] port` on failure.
- A launch probes the endpoint from the host before starting the sandbox, as on macOS. The sandbox firewall's own reachability check (firewall mode's existing destination probe) reports a timeout on the gateway with a hint about host firewalls such as ufw, because spike 0.2 could not be repeated with ufw on the spike host.

## Risks / Trade-offs

- **[Docker Engine: every container on any bridge can reach the sidecar port on the gateway]** → The sidecar grants only what `egress.toml` allows, and the CA is never given to those containers. This is the same exposure as firewall mode's `linux-bridge-socat` listeners. `docs/security.md` compares the two.
- **[Docker Engine: the parent forwarder listens on a non-loopback address]** → Only the sidecar's address is accepted, on a port nothing else uses, and the peer check runs on every connection (spike 0.2: source addresses are preserved).
- **[A host firewall blocks the gateway]** → Untested (no ufw on the spike host); the sandbox's reachability check fails with a hint.
- **[Concurrent sessions sharing a local service on Docker Engine]** → Unchanged firewall-mode limitation; the second session fails its bind check with the existing message. Recorded in TODO.md.
- **[The `docker0` gateway changes]** → It is a restart input; `status` reports drift and launches fail with the restart hint, like port drift.
- **[pasta behaviour varies by build]** → The e2e suite runs on the pasta version of the test host; `doctor` reports the Podman version.

## Migration Plan

- None beyond `egress-sidecar-macos`: sidecar mode on Linux was rejected before. On a Linux host, the first `up` creates the sidecar with the Linux endpoint.
- Rollback: `project-sandbox-egress down`.
