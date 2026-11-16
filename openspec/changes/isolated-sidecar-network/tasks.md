# Tasks

## 0. Spikes (run before any code depends on them)

Each spike records its commands, the macOS, `container`, Colima and Docker versions, the observed output, and a PASS / WORKAROUND / BLOCKED verdict in `openspec/changes/isolated-sidecar-network/spikes.md`. A BLOCKED verdict removes that runtime from scope and goes back to design before any code for it.

- [ ] 0.1 S1, Colima host path and streaming:
  - from a container on `psbx-egress-uplink`, reach a Mac service on `127.0.0.1` and one on `0.0.0.0` through `host.lima.internal` (record the resolved address), on the VZ driver;
  - run Squid with one `accel` port and an `originserver` `cache_peer` to that address;
  - from a container on `psbx-sandbox`, measure first-chunk latency for an Ollama `/api/generate` stream and a 1-event-per-second SSE stream.

  Verify: recorded output showing both requests succeed and chunks arrive incrementally.
- [ ] 0.2 S2, Colima containment without the firewall: from a container on `psbx-sandbox` with no added capabilities and no project firewall, probe `192.168.5.2` on 22, 80, 11434 and a random port, then repeat Spike B's matrix. Then, with `NET_ADMIN`, set the sidecar as the default route and probe again. Verify: a recorded PASS, with every probe refused or timed out.
- [ ] 0.3 S3, Apple internal network: on macOS 26 with the latest `container`, run `container network create --internal` as a normal user (with labels, if supported). From a container on it, probe public IPv4/IPv6 TCP and UDP, DNS for a public name, the host's address on that network, `203.0.113.113`, `192.168.64.1` and `169.254.169.254`. Verify: recorded output, the `container` version that first supports this, and a verdict.
- [ ] 0.4 S4, Apple topology:
  - attach one container to a NAT network and the internal network, and check it reaches the Internet and is reachable from a second container on the internal network;
  - record whether two containers on the internal network reach each other;
  - record whether `container run` supports `--add-host`, and whether network aliases exist;
  - record whether the gateway's internal address changes after stop/start and after recreation;
  - check that `ip_forward=0` in the gateway blocks a default-route bypass.

  Verify: recorded output and a verdict. If containers on a host-only network can't reach each other, the verdict is BLOCKED.
- [ ] 0.5 S5, Apple host path and streaming: from the S4 gateway, reach a Mac loopback service through `host.docker.internal` (the configured alias), then repeat S1's Squid accel and streaming measurements from a sandbox on the internal network. Verify: recorded output showing incremental delivery.
- [ ] 0.6 S6, attribution: on both runtimes, record the client address in Squid's access log for two sandboxes on the internal network. Verify: recorded log lines with distinct per-sandbox addresses (informational; no verdict needed).
- [ ] 0.7 Review the verdicts:
  - update `design.md` D2–D8 for each WORKAROUND, including the minimum `container` version (D8) and the `egress` pinning method (D7);
  - drop a runtime from scope on BLOCKED;
  - record which runtimes count as having verified containment for D6.

  Verify: `design.md` and `spikes.md` updated, and `openspec validate isolated-sidecar-network --strict` passes.

## 1. Policy: `[egress.services]`

- [x] 1.1 Add `[egress.services]` to `egress_policy.py`: name pattern, port range 1024–65535, unique ports, 3128 reserved, unknown-key rejection inside entries. Verify with `tests/test_egress_policy.py` cases for valid tables, duplicates, the reserved port and bad names, each naming the key.
- [x] 1.2 Add `tomlkit` edit helpers for `service add|remove`, re-validated before an atomic replace. Verify with tests that comments and unrelated keys survive, and that an invalid edit leaves the file byte-identical.

## 2. Isolated network backend

- [ ] 2.1 Generalize `colima_network.py` into a per-runtime isolated network backend: argv builders for Docker on Colima and Apple `container` (`network create --internal`, labels, connect/attach), ownership validation and attachment checks. Verify with argv-equality tests showing Colima's argv unchanged, plus new Apple argv tests including the foreign-network refusal.
- [ ] 2.2 Add the Apple version gate (D8): `sw_vers` and `container --version` parsing, checked before any network, key or container work in sidecar mode, and reported by `doctor`. Verify with tests for macOS 15, an old `container`, a passing system, and firewall mode on macOS 15 being unaffected.

## 3. Sidecar: isolated strategy on Apple

- [ ] 3.1 Add the Apple isolated `Strategy`:
  - no `--publish`;
  - attach to `psbx-egress-uplink` and `psbx-sandbox`;
  - `ip_forward=0` (or the S4 equivalent);
  - `exec`-based readiness;
  - `STATE_VERSION` 3 with the strategy recorded (D9).

  Verify with runner tests for absent, running and stale-strategy (replaced) sidecars, and that no host port check is made.
- [ ] 3.2 Reject a non-default `[egress.sidecar] port` on Apple, as on Colima. Verify with a test naming the key.
- [ ] 3.3 Render Squid listeners bound to the sidecar's internal address, discovered inside the container at start (D2). Verify with a rendered-config test and a test of the entrypoint discovery helper.
- [ ] 3.4 Update `docs/egress-sidecar.md` and `docs/runtimes.md` for the Apple topology and the macOS 26 requirement. Verify the documented commands against `--dry-run` output.

## 4. Sidecar services

- [ ] 4.1 Render service routes in `squid.conf.j2` (D3):
  - `accel` ports;
  - `originserver` peers at the recorded host address;
  - `myportname` ACLs;
  - `cache deny`;
  - `CONNECT` and absolute-URI denial on service ports;
  - the forward-port rules restricted to port 3128;
  - upstream `cache_peer_access` excluding services.

  Verify with rendered-config tests and `squid -k parse` in the existing config-parse test.
- [ ] 4.2 Resolve and record the host address per runtime (`host.lima.internal` / `host.docker.internal`) at `up`, as a restart input with drift reporting (D4). Verify with runner tests for the address changing, and with `status` naming the restart.
- [ ] 4.3 Implement `project-sandbox-egress service add|remove|list` with live reload, and add services and the internal address to `status` and `status --json`. Verify with CLI tests, including the published-endpoint runtime message and that a reload runs without `rm`/`run`.
- [ ] 4.4 Add a sidecar-side service probe to `status` and `doctor`, naming a service whose host port doesn't answer and the stale Apple alias. Verify with runner tests.
- [ ] 4.5 Document services in `docs/egress-sidecar.md`: declaration, shared access across sandboxes, Ollama's lack of authentication, firewall mode as the per-session alternative. Verify the documented commands run as written in `--dry-run`.

## 5. Sandbox launch on the isolated strategy

- [ ] 5.1 Build the sandbox plan:
  - attach to `psbx-sandbox` only;
  - pin `egress` (D7);
  - proxy variables `http://egress:3128`;
  - Ollama and agent-proxy URLs on `egress:<port>`;
  - `NO_PROXY` containing `egress`;
  - no `host.docker.internal` anywhere.

  Verify with argv and environment tests for Colima and Apple, and an equality test for Docker Desktop.
- [ ] 5.2 Require service declarations for `--pi-ollama` and loopback `--agent-proxy` on the isolated strategy, failing before container work with the exact `service add` command. Allow remote agent proxies through the forward proxy. Verify with CLI tests for each case.
- [ ] 5.3 Relax the Colima host-feature rejection in sidecar mode only, and name `--egress sidecar` in the firewall-mode error. Verify with tests for firewall mode, sidecar mode and a loopback parent.
- [ ] 5.4 Update `docs/runtimes.md` (the Colima feature matrix and the "Not supported on Colima" list). Verify that the matrix matches the CLI's rejections.

## 6. Sandbox firewall switch

- [ ] 6.1 Add `--sandbox-firewall` / `--no-sandbox-firewall`:
  - mutually exclusive;
  - only valid on the isolated strategy;
  - `--no-sandbox-firewall` refused on unverified runtimes;
  - the per-runtime default from the D6 table.

  Verify with CLI validation tests and the launch message for unverified runtimes.
- [ ] 6.2 Write the firewall decision and the service ports into a root-owned session input file. Add `NET_ADMIN`/`NET_RAW` only when the firewall is on. Verify with session-input and run-argv tests.
- [ ] 6.3 Extend `init-firewall.sh.j2` and `entrypoint.sh.j2` for the isolated strategy (D5):
  - **on:** default-deny with `egress` on 3128 plus the service ports from the session input, no DNS, IPv6 fail-closed;
  - **off:** take the run-once guard, revoke sudo, install nothing.

  Verify with rendered-template tests, and a test that the session input, not the environment, selects the mode.
- [ ] 6.4 Regression test: adding a service and launching with `--no-build` doesn't rebuild, and the opt-in firewall admits the new port. Verify with a build-cache fingerprint test.
- [ ] 6.5 Document the flag, the per-runtime default and the containment matrix in `docs/egress-sidecar.md`. Document lateral reachability and the shared-service exposure in `docs/security.md`. Verify the matrix lists all six sidecar runtimes.

## 7. End-to-end checks

- [ ] 7.1 Extend `scripts/e2e-colima.py` (and `scripts/e2e-egress-sidecar.py` for Apple):
  - repeat the containment probes from S2/S3 without the sandbox firewall;
  - an allowed request through `egress:3128`;
  - Ollama and SSE through a declared service;
  - the service-port pivot denial;
  - a host address through the forward proxy denied;
  - `service add` applied live;
  - sudo revoked with the firewall off.

  Verify: both suites pass on their runtime.
- [ ] 7.2 After 7.1 passes on a runtime, add it to the D6 verified-containment table so its default becomes off. Verify with a unit test of the table and a re-run of that runtime's suite.
- [ ] 7.3 Run `uv run python -m compileall src tests` and `uv run pytest -q`. Verify both pass.

## 8. Roadmap

- [x] 8.1 Add a ROADMAP entry "Namespace-owner gateway for Linux sidecar mode": a per-session privileged gateway container owns the network namespace and its nftables, and the sandbox joins with `--network container:` and no capabilities. It applies to Docker Engine, rootless Podman, Docker Desktop and Podman machine, and is not possible on Apple `container`. Also add "per-sandbox denial attribution from internal-network source IPs" under the existing sidecar follow-ups, citing S6. Verify that `ROADMAP.md` contains both entries.

## Workflow follow-up

- Reconcile with `mount-session-firewall`, `sidecar-policy-hardening` and `scope-firewall-allowlist` when they are brought from `v0.5.x` onto `main`. In particular, the D5 session input becomes the mounted firewall's input.
- Archive the change after review, then sync the deltas into `openspec/specs/`.
