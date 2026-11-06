# Tasks

## 0. Spikes (Apple `container` 1.4.1 and Docker Desktop; record results in design.md)

- [x] 0.1 Run the current sidecar image with `--memory 256M --cpus 1` on Apple `container` and Docker Desktop. Verify readiness and ten concurrent CONNECTs, and record peak RSS. Set the Apple default (256M or 512M) in design.md Decision 12.
- [x] 0.2 From `<runtime> exec`, start a process with `start-stop-daemon --background` in the sidecar. Verify it is still listening after the `exec` returns, on Apple `container`, Docker Desktop and Podman machine.
- [x] 0.3 Keep a CONNECT tunnel streaming for 60 s while running `squid -k reconfigure`. Verify that the stream survives and that new allowlist entries apply. Record the outcome against the Decision 4 risk.
- [x] 0.4 Check `container logs` on Apple for `--since`, tail or size options, and the time to fetch a log of about 50k lines. Record the fetch strategy for Decision 11.
- [x] 0.5 Start a detached host relay (`start_new_session`), close the launching terminal, and verify that the sidecar still reaches it through the `host.docker.internal` localhost alias.

## 1. Policy file

- [x] 1.1 Extend `egress_policy.parse()` with `[egress.upstream]` (`url`, `ca_cert`), `[egress.ca]` (`lifetime_days`, 1–30) and `[egress.sidecar]` (`memory`, `cpus`, `forward_ports`), keeping unknown-key rejection. Verify with `tests/test_egress_policy.py` cases for valid, out-of-range and unknown keys.
- [x] 1.2 Make the effective allowlist `allow` ∪ every supported agent's provider hosts, keeping the GitHub repository host rejection. Verify with tests that a policy allowing only `pypi.org` yields every provider host, and that `*.githubusercontent.com` is still rejected.
- [x] 1.3 Add a pinned `tomlkit` dependency and edit helpers for `allow add|remove` and `forward add|remove`. Each helper re-validates through `parse()` and replaces the file atomically. Verify with tests that comments and unrelated keys survive, and that an invalid edit leaves the file byte-identical.

## 2. Sidecar image and rendered config

- [x] 2.1 Add `client=%>a` to the `sbx` logformat and teach `egress_log` to parse it. Verify with `tests/test_squid_config.py` and `tests/test_egress_log.py` on lines with and without the field.
- [x] 2.2 Add an `sbx-reload` script to the sidecar image: copy `squid.conf`, run `squid -k parse`, then `squid -k reconfigure`. Verify with a renderer test of the image context, and with a test that the image tag changes.
- [x] 2.3 Replace `SBX_FORWARD_PORTS` with a `forwarders` file and an `sbx-forwarders` reconcile script, run by the entrypoint and through `exec`, using `start-stop-daemon` and `range=<subnet>`. Verify with renderer tests of the entrypoint and script, including the port-range validation.
- [x] 2.4 Implement the reload and restart fingerprints (design.md Decision 3), with the reload fingerprint rendered against the running upstream. Verify with tests that an allowlist change affects only the reload fingerprint, and that upstream, Copilot, resources and image changes affect only the restart fingerprint.

## 3. Sidecar CA

- [x] 3.1 Make the CA lifetime configurable (`MAX_LIFETIME` 30 days), name the CA after the runtime, and add a reuse check (at least 2 days left and constraints match). Verify with `tests/test_session_ca.py` cases for reuse, expiry, a constraint change and the cap.
- [x] 3.2 Update `docs/security.md` on CA lifetime and key handling. Verify that the documented paths and limits match the code constants.

## 4. Global sidecar orchestration (`egress_sidecar.py`)

- [x] 4.1 Add the fixed names and labels, the `egress/<runtime>/` layout, and `state.json` read/write that tolerates corruption. Verify with tests that a missing or garbled `state.json` is treated as "needs up".
- [x] 4.2 Build the argv for the reused-or-created `sbx-egress` network and for the sidecar run: resource limits, `--log-opt` on Docker and Podman, the `sidecar/` mount, two networks on Apple. Verify with per-runtime argv tests.
- [x] 4.3 Implement `ensure()` under `path_lock("egress-sidecar", …)` for the absent, running-and-ready, stopped, unready and incomplete-state cases. Verify with injected-runner tests for each case, plus a test in which two threads call `ensure()` and exactly one `run` is issued.
- [x] 4.4 Implement `down()` and `restart(new_ca=…)`. A failed `network rm` with attached endpoints is tolerated, and the key is deleted on `down`. Verify with runner tests and a filesystem check that `ca-key.pem` is gone.
- [x] 4.5 Implement drift handling: reload for allowlist differences and a warning naming each restart-only field. Verify with tests that a reload issues `exec … sbx-reload` and no `rm`/`run`, and that the warnings name the fields.
- [x] 4.6 Implement forwarder reconciliation through `exec … sbx-forwarders`, and the Docker Engine and pasta rejection through the existing `local_service_network` probes. Verify with tests for Desktop (accepted), Engine with forwarders (rejected before `run`) and Engine without forwarders (accepted).
- [x] 4.7 Remove legacy per-session resources (label `project-sandbox.egress.session`) whose session lock is free, both in `ensure()` and from `doctor`. Verify with the existing orphan tests adapted to the new call sites.
- [x] 4.8 Document the shared network and its isolation boundary in `docs/security.md`. Verify that the doc names the firewall rules and Squid denials it relies on.

## 5. Sidecar-owned relay (Apple, remote upstream)

- [x] 5.1 Add the `project_sandbox.egress_relay` entry point: run `BuildProxyForwarder` in the foreground, hold `relay.lock`, and write `relay.pid` and `relay.log`. Verify with a test that starts it on a loopback port in a temp state directory, checks that the lock is held and a CONNECT is relayed, and that SIGTERM releases the lock.
- [x] 5.2 Integrate spawning (`start_new_session`), liveness through the lock, kill-on-`down`, respawn on the recorded port, and the port move with re-render and reload. Verify with fake-spawner tests for each path, including that a pid without a held lock is never signalled.

## 6. `project-sandbox` launch integration

- [x] 6.1 Remove `--egress-policy`. In sidecar mode reject `--extra-domain`, `--internet-proxy` and `--ca-cert` with replacement hints, and check `--agent copilot` against the Copilot setting. Verify with `tests/test_cli.py` cases asserting the messages and that no runner call happens.
- [x] 6.2 Replace `_start_egress_sidecar` with `ensure()` plus `sessions/<sid>/` inputs (`egress.conf`, `ca.pem`, `upstream-ca.pem`). Drop sidecar teardown and the in-process Apple relay in sidecar mode, and have the sandbox firewall step install `upstream-ca.pem`. Verify with argv and rendered-firewall tests, and that the sandbox image checksum does not depend on the upstream CA.
- [x] 6.3 Write only this session's local-service ports into `egress.conf`, and fail when one is not forwarded, naming `forward add`. Verify with tests for a subset, a missing port and no ports.
- [x] 6.4 Add the CA warning (less than 2 days, interception active) and the refusal (expired). Verify with tests using an injected clock, including silence when nothing is intercepted.
- [x] 6.5 Reject launches whose mounts contain `egress.toml` or the state directory, checked after resolving symlinks. Verify with tests for a mounted `~/.config` and a symlinked mount.
- [x] 6.6 Count denials in the session window using the sidecar's clock, and print the count with the `project-sandbox-egress log --denied --since …` hint, or nothing when there are none. Verify with `tests/test_egress_log.py` window tests and a CLI output test.
- [x] 6.7 Show policy, drift and would-start/would-reuse in `--dry-run` from a read-only `inspect`. Verify with a dry-run test asserting that only `inspect`/`ls` argv were issued and no file was written.
- [x] 6.8 Rewrite the "How it works", "Policy" and "Parent proxy" sections of `docs/egress-sidecar.md` for the global sidecar, and add the three-option comparison (firewall, sidecar, your own proxy) to `docs/egress-sidecar.md` and `docs/internet-proxy.md`. Verify that every documented flag and key exists in the parser or policy schema.

## 7. `project-sandbox-egress` executable

- [x] 7.1 Add `project_sandbox/egress_cli.py` with argparse subparsers and register `project-sandbox-egress` in `[project.scripts]`. Verify that `uv run project-sandbox-egress --help` lists every subcommand, and that an unknown subcommand exits 2.
- [x] 7.2 Implement `up`, `down`, `restart [--new-ca]` and `status [--json]` on top of section 4. `down` and `status` print the attached containers. Verify with CLI tests using an injected runner, including `status` issuing no mutating argv.
- [x] 7.3 Implement `allow list|add|remove`, `forward list|add|remove` and `reload`, applied live under the lock. Verify with tests: edit plus reload or reconcile argv, GitHub rejection with the file unchanged, and privileged-port rejection.
- [x] 7.4 Implement `log [--follow] [--denied] [--since TIME|DURATION]`. Verify with parser tests for both `--since` forms and filter tests on sample log lines.
- [x] 7.5 Document the executable in `docs/egress-sidecar.md` ("Managing the sidecar") and link it from `README.md`'s usage pointer. Verify that the documented examples run against `--help` output.

## 8. Roadmap and follow-ups

- [x] 8.1 Record the follow-ups in ROADMAP.md: per-sandbox denial attribution, sidecar IP pinning so sandboxes survive restarts, and a shared host relay for firewall-mode Apple sessions. Update the resolved relay item in ROADMAP.md and TODO.md for the global sidecar. Verify that each item names this change.

## 9. End-to-end verification

- [x] 9.1 Extend the `egress-sidecar` e2e suite: two concurrent sandboxes on one sidecar, reuse across launches, live `allow add` reaching both, `down` cutting both off, a relay surviving the first session (Apple with the fixture parent), and the session-end denial count. Run it on Apple `container` and Docker Desktop and record the results in `docs/development.md`.
- [x] 9.2 Re-run the opt-in `copilot-egress` suite with `[egress.copilot] enabled = true` in the global policy, and record that GitHub repository probes stay denied.

## Workflow follow-up

- Implement `global-egress-sidecar-linux` after this change is archived.
- Archive this change after review, syncing `egress-sidecar`, `egress-sidecar-management` and `internet-proxy-support`.
