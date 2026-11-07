# Proposal

## Why

`egress-sidecar-macos` adds sidecar mode for Apple `container`, Docker Desktop and Podman machine. There the sidecar publishes its proxy on host loopback, and sandboxes reach it through a runtime alias for the host. Linux hosts have no such alias to loopback. Docker Engine's `host-gateway` is the bridge address. Rootless Podman containers live in a separate network namespace. Firewall mode solves this for local services per session: a `socat` on the `docker0` gateway, or pasta's `-T` port forwarding. This change makes sidecar mode work on Docker Engine and rootless Podman with the same mechanisms, and verifies it end to end.

This change depends on `egress-sidecar-macos` and is applied after it has been archived.

## What Changes

- **Docker Engine: the sidecar publishes on the `docker0` gateway.** `docker0` is the default bridge, and the sidecar publishes its port on the bridge's gateway address instead of host loopback. Sandboxes reach it at `host.docker.internal`, mapped to that gateway, with no per-session listener, so concurrent sandboxes share it. Ollama and the agent proxy use firewall mode's `linux-bridge-socat` forwarding, unchanged.
- **Rootless Podman with pasta: the sidecar publishes on host loopback.** Sandboxes reach it through firewall mode's pasta network, which forwards exactly the sidecar port and this session's local-service ports to host loopback. The sidecar itself runs on the same pasta network, with its own private address, so a parent proxy on the host's own LAN address stays reachable.
- **A parent proxy on host loopback works on Linux:**
  - **Rootless Podman:** the sidecar's pasta network also forwards the parent's port.
  - **Docker Engine:** a sidecar-owned host forwarder listens on the `docker0` gateway, accepts only the sidecar's address, and connects to the parent on host loopback. It is supervised like the Apple relay.
- **Rootless Docker is rejected** in sidecar mode with a clear message.
- **Linux verification:** the `egress-sidecar` e2e suite runs on Docker Engine and rootless Podman, including a loopback-only local service and a loopback parent proxy.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `egress-sidecar`:
  - the published endpoint is the `docker0` gateway on Docker Engine;
  - sidecar mode is supported on Docker Engine and rootless Podman, and rejected on rootless Docker;
  - a loopback parent proxy is supported on Linux;
  - the e2e requirement covers the Linux runtimes.

## Impact

- **Code:**
  - `egress_sidecar.py`: the per-runtime publish address and sidecar network, Docker Engine gateway discovery, rootless Docker detection.
  - `egress_relay.py`: a TCP-forward mode with a peer check, bound on the `docker0` gateway.
  - `cli.py`: sidecar-mode planning on Docker Engine (gateway endpoint instead of a socat plan for the sidecar port).
  - `local_service_network.py`: the existing probes and pasta network argument are reused.
- **Security:**
  - On Docker Engine, the sidecar port and the parent forwarder listen on the `docker0` gateway. Every container on any Docker bridge can reach that address (spike 0.2). The sidecar grants only what `egress.toml` allows. The parent forwarder closes every peer except the sidecar.
  - `docs/security.md` compares this with firewall mode's `linux-bridge-socat` listener.
- **Tests:**
  - unit tests for argv, strategy selection and the forwarder's peer check;
  - e2e runs on Docker Engine and rootless Podman.
- **Out of scope:**
  - Rootless Docker.
  - Rootful Podman, which is already refused.
  - Concurrent firewall-mode or sidecar-mode sessions on Docker Engine sharing one local service. `linux-bridge-socat` binds per session, an existing limitation that is recorded in TODO.md.
  - ufw and other host firewalls beyond a clear error.
