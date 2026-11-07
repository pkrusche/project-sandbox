# Tasks

Spikes for the endpoint and the pasta sidecar ran on 2026-11-07 (design.md Decision 1). Unit tests run anywhere; e2e needs a Linux host with Docker Engine and rootless Podman with pasta.

## 1. Strategy selection

- [ ] 1.1 Add the per-runtime endpoint strategy (design.md Decision 1 table) on top of the existing probes, including rootless Docker detection, and replace the macOS change's Linux rejection with it. Verify with tests over fixture `docker info` and `podman info` outputs for each row, including the rootless Docker message.

## 2. Docker Engine

- [ ] 2.1 Read the default bridge's gateway at `up`, record it in `state.json` as a restart input, and publish the sidecar on `<gateway>:<port>:3128`. Verify with argv tests, a port-free check on the gateway, and a drift test for a changed gateway.
- [ ] 2.2 Plan the sidecar endpoint in sidecar-mode sandboxes on Docker Engine as a gateway destination without `socat` (`--add-host host.docker.internal:<gateway>`, firewall destination `<gateway>:<port>`), keeping `linux-bridge-socat` for local services. Verify with run-argv and rendered-firewall tests, and that no socat plan exists for the sidecar port.
- [ ] 2.3 Add the TCP-forward mode to `egress_relay`: bind `<gateway>:0`, check the peer against the recorded sidecar address, forward to `127.0.0.1:<parent port>`, and record `relay.port`. Verify with loopback-based tests: an allowed peer is relayed, a wrong peer is closed without an upstream connect, and SIGTERM releases the lock.
- [ ] 2.4 Wire the parent forwarder into `ensure()`, `down()` and launches for a loopback parent on Docker Engine: start it after `run`, render the parent as `host.docker.internal:<relay.port>` with `--add-host host.docker.internal:<gateway>`, restart it on every cold start, and re-render and reload on a port move. Verify with fake-spawner tests for each path and a `status` test that reports it.

## 3. Rootless Podman with pasta

- [ ] 3.1 Run the sidecar on firewall mode's pasta network argument with `--publish 127.0.0.1:<port>:3128` and `host.docker.internal` mapped to `127.0.0.1`, adding `-T,<parent port>:<parent port>` for a loopback parent. Verify with argv tests with and without a loopback parent.
- [ ] 3.2 Plan sidecar-mode sandboxes on pasta with the sidecar port added to the `-T` list next to this session's local services. Verify with run-argv tests that the parent port is never in a sandbox's list.

## 4. Documentation

- [ ] 4.1 Document the Linux endpoints, the parent forwarder and the rootless Docker rejection in `docs/egress-sidecar.md`, and compare the gateway listeners with firewall mode's `linux-bridge-socat` in `docs/security.md`. Verify that the documented addresses, ports and messages match the code.
- [ ] 4.2 Record in TODO.md the per-session `linux-bridge-socat` bind conflict for concurrent sessions sharing a local service on Docker Engine, and the untested ufw case. Verify that both entries name this change.

## 5. End-to-end verification

- [ ] 5.1 Run the `egress-sidecar` e2e suite on Docker Engine and on rootless Podman with pasta, adding the loopback-only parent probe (with a sandbox-side refusal check) to the Linux runs. Record the results in `docs/development.md`.

## Workflow follow-up

- Apply only after `egress-sidecar-macos` is archived; this change's MODIFIED delta targets requirements that change introduces.
- Archive this change after review, syncing `egress-sidecar`.
