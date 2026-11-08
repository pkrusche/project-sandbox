# Tasks

Unit tests use real sockets on `127.0.0.1` and real `flock`s in a temporary state directory, but no container runtime. The e2e task needs a Linux host with Docker Engine.

## 1. Generalize the relay forwarder

- [x] 1.1 In `egress_relay._ForwardServer`, make `peer` optional, accept a `target_host` of `127.0.0.1` or `::1` (connecting with the matching address family), add an exact-port bind with no free-port fallback, and set `SO_REUSEADDR` on the listener (design.md Decision 3). Verify with tests: no filter relays any peer; `::1` upstream is relayed (skipped when the host has no IPv6 loopback); exact port in use raises; the existing peer-filter tests still pass.
- [x] 1.2 Expose these on the CLI as `--target-host` and `--exact-port`, with `--peer` optional only when `--exact-port` is given, and add the matching `RelayControl` spawn method. Verify with `main()` argument tests that `--forward` without `--peer` and without `--exact-port` is rejected, and an argv test for the new spawn method.
- [x] 1.3 Parameterize `_run_locked`'s state-file names, and add an optional `idle` callback that runs every `IDLE_POLL` and stops the server when it returns true. Verify that the sidecar relay's files and behaviour are unchanged (existing tests) and that a test `idle` callback stops the server and releases the lock.

## 2. Lease and attach protocol

- [x] 2.1 Create `local_forwarder.py` with the state layout under `$XDG_STATE_HOME/project-sandbox/local-forward/<gateway>-<port>/` (directory `0700`, files `0600`, `O_NOFOLLOW`), and the files `fwd.lock`, `lease`, `ctl.lock`, `upstream` (including the project-sandbox version), `fwd.pid` and `fwd.log` (design.md Decision 1). Verify with tests for the modes and for refusing a symlinked state file.
- [x] 2.2 Implement the forwarder-side idle check (`ctl.lock` non-blocking, then `lease` `LOCK_EX|LOCK_NB`) and wire it in as the `idle` callback, writing `upstream` before the forwarder reports ready. Verify with tests that a forwarder with no lease exits within about two polls and that one with a held lease keeps serving.
- [x] 2.3 Implement session attach: take `ctl.lock`; on a live forwarder, check the upstream and version; otherwise probe-bind and spawn; take a `LOCK_SH` lease on an `O_CLOEXEC` fd; release `ctl.lock`. Verify with tests for:
  - two leases sharing one forwarder process;
  - an upstream mismatch error naming both addresses;
  - a version-mismatch error;
  - a foreign listener producing "Cannot bind … another process owns";
  - `os.get_inheritable(lease_fd) is False`.
- [x] 2.4 Add lifecycle tests with a real spawned forwarder:
  - the spawning session releases first and the remaining session keeps relaying new connections;
  - a session subprocess killed with SIGKILL releases its lease, and the forwarder exits;
  - an attach that blocks on `ctl.lock` while the forwarder is exiting ends up on a live forwarder (force the interleaving with a held `ctl.lock`).

  Verify all of these pass under `uv run pytest -q tests/test_local_forwarder.py`.

## 3. Switch Docker Engine planning to the shared forwarder

- [x] 3.1 In `local_service_network.py`, rename the strategy to `linux-bridge-forwarder`, make `ForwardingPlan.start()` attach and `close()` release the lease, and change `prepare()`/`prepare_services()`'s bind check to accept our live forwarder with a matching upstream. Remove the socat spawn and `_die_with_parent_hook`. Verify with updated `tests/test_local_service_network.py`: a plan with no socat argv, the early refusal for a foreign listener, and acceptance of a matching live forwarder.
- [x] 3.2 Update `cli.py`: the sidecar-mode comment and strategy references, and remove the "spawn before the relay thread" constraint comment (design.md Decision 5). Verify with `tests/test_cli.py` session-argv tests and a regression test that two prepared sessions for the same Ollama port both start their plans without error (with the forwarder spawn faked).
- [x] 3.3 Update the dry-run and `--verbose` output for the new strategy name. Verify with dry-run tests that nothing is spawned and no state directory is created.

## 4. Remove socat and update docs

- [x] 4.1 Remove `doctor.check_socat()` and its tests, and update `docs/doctor.md`. Verify that `uv run project-sandbox doctor` no longer lists socat and that `tests/test_doctor.py` passes.
- [x] 4.2 Update the e2e cleanup helper (`tests/test_e2e_cleanup.py`) to find leftover `local-forward` forwarders instead of socat processes, and update `docs/development.md`. Verify with the cleanup helper's tests.
- [x] 4.3 Update `docs/runtime.md`, `docs/usage.md`, `docs/egress-sidecar.md` and `docs/apple-container-dns.md` to describe the shared bridge forwarder instead of per-session `socat`. In `docs/security.md`, update the listener comparison table, including the lifetime (the union of the sessions that use it, plus about one poll interval). Verify with `grep -rn socat docs README.md` that the only remaining matches are deliberate historical notes.
- [x] 4.4 Remove the concurrent-sessions item from TODO.md. Verify that TODO.md no longer mentions `linux-bridge-socat`.

## 5. Integration verification

- [x] 5.1 Run `uv run python -m compileall src tests` and `uv run pytest -q`, and verify both pass.
- [ ] 5.2 On a Docker Engine host without `socat` on `PATH`, start two concurrent `--agent pi --pi-ollama` sessions, and check each of the following:
  - both reach Ollama;
  - ending the first does not break the second;
  - the forwarder process is gone a few seconds after the second ends;
  - a session with `kill -9` releases correctly.

  Record the results in `docs/development.md`.

## Workflow follow-up

- Run `/opsx:archive` after review to sync the `local-service-forwarding` and `local-ollama-support` deltas into `openspec/specs/`.
