# Design

## Context

See proposal.md (Why) for the problem. Current state:

- `local_service_network.prepare()` picks `linux-bridge-socat` only on Docker Engine (not Desktop). It reads the gateway with `docker network inspect bridge` and checks with `_validate_bindable()` that `<gateway>:<port>` is free. `prepare_services()` repeats that check for each additional port.
- `ForwardingPlan.start()` spawns `socat TCP-LISTEN:<port>,bind=<gateway>,reuseaddr,fork <upstream>` as a child with `start_new_session` and a PDEATHSIG `preexec_fn`. `cli.py` starts every plan before the Internet-proxy relay thread exists, because `preexec_fn` is only fork-safe while the parent has one thread. It closes the plans in reverse order in `finally`.
- `egress_relay` already runs a detached, lock-supervised forwarder for sidecar mode (`--forward --bind --peer --target-port --port`). It holds `relay.lock` exclusively for its whole life (`session_ca.acquire_exclusive`), and its liveness is checked with `session_ca.lock_held`, never with a pid. `_ForwardServer` binds IPv4 only, connects to `127.0.0.1`, filters on one peer, and falls back to a free port.
- The sandbox side does not change. `host.docker.internal` is mapped to the gateway, and the firewall admits `<gateway>:<port>`.

## Goals / Non-Goals

**Goals:**
- No supervisor or daemon beyond the forwarder process itself. Correctness comes from kernel `flock` semantics, so crashes need no cleanup.
- One forwarder implementation shared by the sidecar parent forwarder and local services.

**Non-Goals:**
- Telling sandboxes apart at the forwarder. Like `socat`, it accepts every peer on the bridge.
- An explicit `stop`/`status` command for local forwarders. They exit on their own. `RelayControl.stop` stays available for tests and e2e cleanup.

## Decisions

### 1. Lease protocol on three files

The state directory is `$XDG_STATE_HOME/project-sandbox/local-forward/<gateway>-<port>/`, created with mode `0700`. All opens use `O_NOFOLLOW`.

```
fwd.lock   forwarder: LOCK_EX for its whole life   → liveness
lease      every session: LOCK_SH for its whole life
ctl.lock   LOCK_EX briefly: serializes attach (session) against exit (forwarder)
upstream   "<loopback host>\n", written by the forwarder before it reports ready
fwd.pid, fwd.log                                    (as relay.pid / relay.log)
```

**Session attach** (at the point where `ForwardingPlan.start()` runs today):
1. Take `ctl.lock` (blocking, with a bounded wait).
2. If `fwd.lock` is held (`lock_held`): read `upstream` and refuse on a mismatch (spec: one upstream).
   Otherwise: probe-bind `<gateway>:<port>`. On failure, a foreign process owns the port, so fail with the "Cannot bind … another process" error. On success, spawn the forwarder detached and wait until it reports ready (`fwd.lock` held and `upstream` written), up to `SPAWN_TIMEOUT`.
3. Open `lease` and take `LOCK_SH`. Keep the fd open in the CLI until the session ends.
4. Release `ctl.lock`.

**Forwarder exit check** (a poll every `IDLE_POLL = 2s`):
1. Take `ctl.lock` (non-blocking; if busy, a session is attaching, so skip this round).
2. Try `LOCK_EX | LOCK_NB` on `lease`. If it succeeds, no session holds a lease: close the listener, remove `fwd.pid`, and exit while still holding `ctl.lock`. The kernel releases every lock on exit.
3. Otherwise release `ctl.lock` and keep serving.

Why this is race-free:
- A session that holds `ctl.lock` while it spawns cannot be outrun by a check, because the new forwarder's check needs `ctl.lock`, and by then the session holds its lease.
- A session that arrives during an exit waits on `ctl.lock`. It then finds `fwd.lock` free (the forwarder released it on exit) and spawns a new forwarder, matching the scenario "Session starts while the forwarder is stopping".
- A crashed session's `LOCK_SH` is released by the kernel, so there is no refcount that can leak.

There's one subtlety to watch. Any momentary `LOCK_SH` probe on `lease` by another process just makes an exit check fail, which is the safe direction. The forwarder must take `fwd.lock` with `acquire_exclusive`, as the relay does, so that `lock_held` probes on it do not make it think another forwarder is running.

*Alternatives considered:*
- **A refcount file with pids.** This needs stale-pid detection and is exposed to pid reuse. The codebase deliberately avoids pid-based liveness.
- **A forwarder that never exits.** Simpler, but it leaves a listener on the gateway indefinitely after the last session, a larger exposure window than today.
- **One forwarder process for all ports.** Fewer processes, but the lifetime gets coupled across services and attach needs an IPC channel. The per-port design reuses the relay model unchanged.

### 2. Lease fd must not leak into children

The CLI spawns runtime processes (`docker run`, the firewall check, and so on). A lease fd inherited by a long-lived child would keep the forwarder alive after the CLI dies. Python fds are non-inheritable by default, and `subprocess` uses `close_fds=True` by default. The lease is opened with `os.open(..., O_CLOEXEC)` explicitly, and a test asserts it is not inheritable (`os.get_inheritable(fd) is False`).

### 3. Generalize `egress_relay._ForwardServer`

- `peer: str | None`. `None` disables the filter. The CLI flag `--peer` becomes optional only together with a new `--exact-port` flag, so that the sidecar parent forwarder cannot lose its filter by accident.
- `target_host` is accepted on the CLI (`--target-host`), validated to be `127.0.0.1` or `::1`. The upstream connection uses the matching address family.
- `--exact-port`: bind only the requested port; there is no `(port, 0)` fallback. A bind failure exits non-zero with the reason in `fwd.log`, which the spawning session reports.
- `SO_REUSEADDR` on the listener, matching socat's `reuseaddr`, so TIME_WAIT sockets from a just-exited forwarder do not block a respawn.
- The idle exit check is a new optional `idle` callback in `_run_locked`'s wait loop: wait on `stop` with a timeout of `IDLE_POLL`, then run the check. The sidecar relay passes none, so its behaviour is unchanged.
- The lock name and state-file names are parameters, so `local-forward/` uses `fwd.*` and the lease and control files, while `relay.*` keeps its current names.

*Alternative:* a new standalone forwarder module. Rejected: it would duplicate the bind, splice and shutdown logic that `_ForwardServer` already tests, plus the detached-spawn logic in `RelayControl._start`.

### 4. Where the code lives

A new module, `local_forwarder.py`, holds the state layout, the attach and lease logic, and the forwarder-side idle check. It uses `egress_relay` for the server and the spawn. `ForwardingPlan` keeps its strategy name (`linux-bridge-socat` becomes `linux-bridge-forwarder`; `describe()` output and tests are updated). `start()` attaches, and `close()` releases the lease by closing the fd. `prepare()`'s `_validate_bindable` becomes "bindable, or held by our live forwarder with a matching upstream". That keeps the early, pre-build failure for a foreign listener, while step 2 of the attach repeats the check under `ctl.lock` as the authoritative one.

### 5. Drop PDEATHSIG and the spawn-ordering constraint

The forwarder is meant to outlive its spawner, so `_die_with_parent_hook` and its `preexec_fn` are removed. Without `preexec_fn`, spawning is fork-safe from a multi-threaded parent, and the ordering comment in `cli.py` goes away (the order itself can stay).

### 6. Remove socat

`doctor.check_socat()` and its row in `docs/doctor.md` are removed. The e2e cleanup helper (`tests/test_e2e_cleanup.py`, `docs/development.md`) looks for leftover `project_sandbox.egress_relay` forwarders with a `local-forward` state directory instead of socat processes.

## Risks / Trade-offs

- **[A listener lives slightly longer than any session]** → It is bounded by `IDLE_POLL`, about 2s after the last lease drops. The exposure is otherwise the same as `socat`'s: reachable from every container on any Docker bridge, forwarding to one loopback port. `docs/security.md` records the changed lifetime.
- **[A session waits on `ctl.lock` for a slow spawn]** → The wait is bounded by `SPAWN_TIMEOUT`, and the error includes the `fwd.log` tail, as relay spawn failures do.
- **[A forwarder from an older project-sandbox version, with different flags or behaviour]** → The forwarder writes its version next to `upstream`. Attach treats a mismatch like an upstream mismatch: it refuses with "a forwarder from another project-sandbox version is running; wait for its sessions to end". It does not kill a forwarder other sessions rely on.
- **[Docker restarts and the gateway changes while forwarders run]** → The state directory is keyed by gateway, so new sessions get new forwarders. Old ones keep their now-dead address until their sessions end, and then exit normally.
- **[The `$XDG_STATE_HOME` filesystem does not support `flock`, for example some network home directories]** → The egress sidecar has the same constraint today. Attach fails with the lock error and the path.
- **[Data in transit at forwarder exit]** → Exit happens only when no session holds a lease, so any open connection belongs to a sandbox whose session has ended.

## Migration Plan

There is no persisted state to migrate. An upgraded CLI no longer starts `socat`. A session from an older version that is still running keeps its own `socat` on the port, and a new session sees a foreign listener and fails as it does today, until the old session ends. Rollback means reverting the release; stale `local-forward/` directories are harmless.

## Open Questions

- The exact `IDLE_POLL` and the `ctl.lock` wait bound can be tuned during implementation without changing the specs.
