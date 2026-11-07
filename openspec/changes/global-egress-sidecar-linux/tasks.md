# Tasks

## 0. Spikes (Linux host with Docker Engine and rootless Podman 5.3+; record results in design.md)

- [x] 0.1 On rootless Podman with pasta, attach a container to a netavark bridge and to an `--internal` network, and connect to `host.containers.internal:<port>` where a service listens only on host `127.0.0.1`. Record whether it connects, and on which Podman versions. If it fails, reduce the spec delta to Docker Engine before continuing (design.md Decision 3).
- [ ] 0.2 On Docker Engine, create a user bridge, bind a host listener on its gateway, and verify that a container on that bridge connects while a container on `docker0` cannot. Repeat with ufw enabled and record the result for the Decision 4 risk.

## 1. Strategy selection

- [ ] 1.1 Add the per-runtime forwarding strategy (design.md Decision 4) on top of the existing probes, including rootless Docker detection and Podman version gating. Verify with tests over fixture `docker info` and `podman info` outputs for each row of the table, including the rejection messages.

## 2. Docker Engine uplink and host forwarders

- [ ] 2.1 Create and reuse `sbx-egress-uplink` on Docker Engine, run the sidecar on it with `--add-host host.docker.internal:<gateway>`, and remove it in `down` when nothing is attached. Verify with argv tests, and with a `down` test that tolerates attached endpoints.
- [ ] 2.2 Rename `egress_relay` to `egress_host_helper` and add the TCP forwarder component: bind on the gateway, check the peer against the sidecar IP, forward to `127.0.0.1:<port>`, reload on SIGHUP. Verify with loopback-based tests: an allowed peer is relayed, a wrong peer is closed without an upstream connect, and SIGHUP adds and removes listeners.
- [ ] 2.3 Wire the helper into `ensure()`, `down()` and `forward add|remove`: spawn it when there are forwarders on Docker Engine, send SIGHUP after reconciling, restart it on a gateway change. Verify with fake-spawner tests, and with a restart-fingerprint test for the network layout.
- [ ] 2.4 Extend `project-sandbox-egress status` and `forward list` with the host forwarders' state. Verify with CLI output tests, including `--json`.
- [ ] 2.5 Document the uplink network and the host forwarders in `docs/security.md` and `docs/egress-sidecar.md`, and compare them with firewall mode's `docker0` listener. Verify that the documented network names and ports match the code.

## 3. Rootless Podman with pasta

- [ ] 3.1 Set `SBX_FORWARD_HOST=host.containers.internal` for rootless Podman 5.3 or later with pasta, and drop the macOS change's rejection for that case. Verify with argv tests, and with a test that slirp4netns and older Podman are still rejected with the documented message.
- [ ] 3.2 Add a readiness check that probes one forwarded port from inside the sidecar and fails with a host-firewall or network-mode hint. Verify with injected-runner tests for success and failure.
- [ ] 3.3 Document the Podman requirements (version, pasta) in `docs/egress-sidecar.md`. Verify that the documented version matches the gating constant.

## 4. End-to-end verification

- [ ] 4.1 Run the `egress-sidecar` e2e suite on Docker Engine and on rootless Podman with pasta, adding a probe against a service that listens only on host loopback. Record the results in `docs/development.md`, and remove the pasta verification item from TODO.md.
- [ ] 4.2 Record in ROADMAP.md that firewall mode's `linux-bridge-socat` could move behind a private network in the same way. Verify that the entry names this change.

## Workflow follow-up

- Apply only after `global-egress-sidecar-macos` is archived. This change's RENAMED/MODIFIED delta targets a requirement that change introduces.
- Archive this change after review, syncing `egress-sidecar`.
