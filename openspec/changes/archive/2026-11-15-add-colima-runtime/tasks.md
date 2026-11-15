# Tasks

## 1. Spikes (run before any code depends on them)

Each spike records its commands, the macOS / Colima / Docker versions, the observed output and a PASS / WORKAROUND / BLOCKED verdict in `docs/colima-spikes.md`. A BLOCKED verdict stops the change and goes back to design.

- [x] 1.1 Spike A, alias on an internal network: create `psbx-egress-uplink` and an `--internal` `psbx-sandbox`, run a Squid container on the uplink, `docker network connect --alias egress` it to the internal network, and from a container on the internal network resolve and use `egress:3128` for an allowed request. Verify with the recorded commands and output.
- [x] 1.2 Spike B, bypass checks: from a container on the internal network, verify that direct TCP, UDP, DNS and IPv6 to public addresses, the VM's bridge gateway, the sidecar's uplink address and another bridge's address all fail; inspect `ip_forward` and forwarding rules in the sidecar to confirm it does not route. Verify with recorded output and a PASS verdict.
- [x] 1.3 Spike C, build proxy and CA: build the sidecar image in a Colima profile behind an intercepting proxy using the existing build-proxy and certificate options. Verify that the build succeeds or record the required workaround.
- [x] 1.4 Spike D, mounts: with a default Colima profile on the VZ driver (and QEMU if available), verify that a workspace, a generated staging directory, a credential file and a `--branch` worktree's main `.git` are visible at the same path in a container, and that symlinks and Git behave. Record which paths the profile exports by default.
- [x] 1.5 Review the spike verdicts and update `design.md` D1–D8 for any WORKAROUND; stop here if any verdict is BLOCKED.

## 2. Context-aware runtime

- [x] 2.1 Add `Runtime.context`, a Colima runtime factory, `--runtime colima` and `--colima-profile` with validation (rejected with other runtimes, invalid names, never chosen by `auto`) in `container_cli.py` and `cli.py`; add tests for each rejection and for `auto` precedence.
- [x] 2.2 Add `cli_argv()` and move the 34 `[runtime.binary, ...]` call sites to it. Verify with a test that argv for Apple `container`, Docker and Podman is unchanged and that Colima argv carries `--context colima-<profile>`.
- [x] 2.3 Implement profile verification (context exists, engine answers, engine belongs to the profile) with `colima start --profile NAME` guidance, and skip it under `--dry-run`. Verify with mocked-runner tests for stopped, missing and foreign contexts, and a dry-run test that prints context-qualified commands without running anything.
- [x] 2.4 Verify that the user's current Docker context and `DOCKER_HOST` are neither changed nor used (test with a mocked runner that asserts every call is context-qualified).
- [x] 2.5 Add the mount-visibility preflight and its error messages, and create the hidden-VCS mask directory (`vcs_access.new_mask_dir()`) under the state directory on Colima instead of `$TMPDIR`. Verify with mocked tests for a visible and an unexported workspace, staging directory and `--branch` `.git`, and a test that the Colima mask directory is under the state directory.
- [x] 2.6 Document runtime selection, setup (`colima start --profile project-sandbox`, mounts) and the explicit context in `docs/colima.md`, with a pointer from `docs/runtime.md`. Verify that every documented command is run exactly as written against a Colima profile.

## 3. Private-network sidecar

- [x] 3.1 Add network helpers: create-if-absent with ownership and profile labels for `psbx-egress-uplink` and the `--internal` `psbx-sandbox`, label validation that refuses a foreign network of the same name, and removal helpers used only by documented manual cleanup. Verify with mocked-runner tests for create, reuse, foreign network and already-exists races.
- [x] 3.2 Add the `DOCKER_PRIVATE_NETWORK` strategy: `endpoint_strategy()` selection, `sidecar_run_argv` without `--publish` and with `--network`, the `network connect --alias egress` step inside `ensure()` under the existing lock, in-container readiness only, and recorded-strategy support. Remove the interim refusal of `--egress sidecar` with `--runtime colima` in `_validate_egress_args` (added in group 2 so Colima could not fall through to a host-published endpoint) and its guard test. Verify with argv tests that no `--publish` appears and that the network connect happens before readiness is reported.
- [x] 3.3 Scope sidecar state, CA, locks and image tags by `colima-<profile>`; skip `port_free`/`probe_published`; leave the port out of the Colima restart fingerprint. Verify with tests for two profiles that keep separate layouts and for `down` on one profile.
- [x] 3.4 Reject a non-default `[egress.sidecar] port` on Colima (launch and `project-sandbox-egress up`), leaving other runtimes' behavior unchanged. Verify with tests for Colima (error naming the key) and Docker (port honored).
- [x] 3.5 Build the sandbox plan: attach the sandbox to `psbx-sandbox` only, read the sidecar's address on that network, pass `--add-host egress:<ip>`, set proxy variables to `http://egress:3128`, and give the firewall only that address and port with no DNS. Fail before starting when the address is unavailable. Verify with rendered-firewall and argv tests, and a test that no `host.docker.internal` mapping appears.
- [x] 3.6 Reject host-dependent features on Colima (Ollama, local agent proxy, loopback upstream parent) before any container work, and never start the parent forwarder or Apple relay. Verify with tests for each rejection and for an address-reachable parent.
- [x] 3.7 Keep `--egress firewall` on Colima identical to Docker Engine. Verify with a test comparing rendered output and argv, other than the context.
- [x] 3.8 Update `docs/egress-sidecar.md` and `docs/colima.md` with the topology, the supported/unsupported matrix, the shared-network trust note, restart behavior, and manual cleanup commands; add a README pointer and a CHANGELOG entry. Verify the documented commands against a Colima profile.

## 4. End-to-end verification

- [x] 4.1 Add an opt-in Colima end-to-end suite (skipped without a running profile): allowed and denied requests through the sidecar, the bypass checks from Spike B as automated tests, two concurrent sandboxes, a sidecar recreate followed by a new launch, and `--egress firewall`. Verify by running it against a profile and recording the result.
- [x] 4.2 Add Colima checks to `doctor` (profile reachable, context correct, mounts, networks owned), non-mutating. Verify that `doctor` and `--dry-run` make no changes with a test and a manual run.
- [x] 4.3 Run `uv run python -m compileall src tests` and `uv run pytest -q`, and run `openspec validate add-colima-runtime --strict`. Verify that all pass.

## Workflow follow-up

- Archive the change after review and after the end-to-end suite passes on a Colima profile.
