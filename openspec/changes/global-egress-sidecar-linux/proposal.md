# Proposal

## Why

`global-egress-sidecar-macos` makes the global egress sidecar forward local services (agent proxy, Ollama) only where the runtime provides a `host.docker.internal` that reaches host loopback: Apple `container`, Docker Desktop and Podman machine. On Linux hosts nothing provides that path to loopback-only services. On Docker Engine, `host-gateway` reaches the bridge address, not `127.0.0.1`. On rootless Podman the container lives in a separate network namespace. Today a Linux sidecar with forwarders is therefore rejected. This change makes sidecar mode on Linux work as it does on macOS and verifies it end to end. That closes the open TODO about the pasta strategy, which has never been tested.

This change depends on `global-egress-sidecar-macos` and is applied after it has been archived.

## What Changes

- **Docker Engine: a private uplink network.** On Docker Engine the sidecar's egress side moves from the default `bridge` to a dedicated bridge network, `sbx-egress-uplink`, which only the sidecar joins.
- **Docker Engine: sidecar-owned host forwarders.** The sidecar-owned host process introduced for the Apple relay also runs one TCP forwarder per configured port, bound to the uplink gateway address. It accepts connections only from the sidecar's address and forwards them to the service on host loopback. The sidecar resolves `host.docker.internal` to that gateway. No host `socat` is needed in sidecar mode.
- **Rootless Podman with pasta:** the sidecar reaches host loopback through Podman's pasta host-address mapping for `host.containers.internal`, which requires Podman 5.3 or later. Older Podman, and slirp4netns, keep rejecting forwarders, naming the requirement.
- **Rootless Docker** keeps rejecting forwarders with a clear message. Its network namespace has the same problem and is out of scope.
- **Live edits keep working:** `project-sandbox-egress forward add|remove` reconciles the host forwarders live, as it does the in-sidecar ones. `status` reports their state.
- **Linux verification:** the `egress-sidecar` e2e suite runs on Docker Engine and rootless Podman (pasta), including local-service forwarding. That replaces the pasta TODO item.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `egress-sidecar`:
  - local-service forwarding through the global sidecar extends to Docker Engine and rootless Podman with pasta;
  - Docker Engine gets a private uplink network and sidecar-owned host forwarders that only the sidecar can reach;
  - the e2e requirement covers Linux runtimes.

## Impact

- **Code:**
  - The sidecar-owned host process (`egress_relay` from the macOS change) becomes a host helper that can also run TCP forwarders.
  - `egress_sidecar.py`: uplink network on Docker Engine, `--add-host` for the sidecar, a per-runtime forwarding strategy, Podman version gating.
  - `local_service_network.py` probes are reused.
  - `egress_cli.py`: `status` and `forward` include the host forwarders.
- **Security:**
  - Host forwarders listen on a bridge address, not loopback. They are reachable only from the uplink network, which only the sidecar joins, and they accept only the sidecar's IP.
  - `docs/security.md` documents this alongside the firewall-mode `linux-bridge-socat` listener, which is reachable from any container on `docker0`.
- **Tests:**
  - unit tests for argv, strategy selection and the forwarder's peer check;
  - Linux e2e runs on Docker Engine and rootless Podman.
- **Out of scope:**
  - Rootless Docker.
  - Rootful Podman, which is already refused.
  - Moving firewall mode's `linux-bridge-socat` behind a private network. Noted in ROADMAP.
