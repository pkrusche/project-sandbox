# Proposal

## Why

On Docker Engine, firewall mode and sidecar mode reach host-loopback services (local Ollama, a loopback `--agent-proxy`, a loopback `--internet-proxy`) through `linux-bridge-socat`. Each session starts its own `socat` bound to `<docker0 gateway>:<service port>`. The address and port are fixed, because every sandbox's rendered configuration, proxy environment and firewall rule uses `host.docker.internal:<service port>`. So a second concurrent session that uses the same service fails its bind check ("Cannot bind the … proxy to 172.17.0.1:<port>"). This is the last item in TODO.md, deferred from `egress-sidecar-linux`.

The listener cannot simply be reused, because it belongs to the session that started it. It is that CLI's child, it dies with it (PDEATHSIG), and it is terminated in that session's cleanup. A second session that borrowed it would lose the service when the first session ends.

## What Changes

- **A shared, leased forwarder per bridge address and port.** On Docker Engine, the per-session `socat` listener is replaced by one detached forwarder per `(gateway, port)`. It is spawned by the first session that needs it and shared by every concurrent session that uses that service. Each session holds a lease for its whole life. The forwarder exits shortly after the last lease is released, including leases released by a crashed session.
- **The forwarder is the existing `egress_relay` TCP-forward process.** It runs in the same process model (detached, lock-based liveness, log file), with three changes:
  - the sidecar-only peer filter becomes optional, and is off for local services;
  - the target can be `127.0.0.1` or `::1`;
  - the bind uses the exact requested port, with no free-port fallback.
- **One upstream per forwarder.** A forwarder records which loopback address it forwards to. A session that needs the same port on a different loopback address fails with a clear error instead of being connected to the wrong service.
- **socat is no longer required.** Docker Engine stops using `socat`. `project-sandbox doctor` no longer reports it as a networking prerequisite.
- **Unchanged error for a foreign listener.** If a process other than a project-sandbox forwarder holds the address and port, startup still fails before the container launches with the existing "Cannot bind the … proxy" message, now saying that another process owns the port.
- TODO.md's concurrent-sessions item is removed.

## Capabilities

### New Capabilities

- `local-service-forwarding`: how host-loopback services are forwarded to sandboxes on Linux bridge runtimes. It covers sharing one forwarder between concurrent sessions, its lease-bounded lifetime, the upstream consistency check, and the failure when a foreign listener holds the port. The service-specific specs (Ollama, agent proxy, Internet proxy) keep owning *which* service gets forwarded. This capability owns *how* the bridge forwarder behaves.

### Modified Capabilities

- `local-ollama-support`:
  - "Ollama remains bound to host loopback" no longer names `socat` as the Linux bridge fallback; it now names the managed bridge forwarder.
  - "Ollama forwarding resources have a bounded lifecycle" drops the `socat`-availability scenario. Its lifecycle now releases this session's lease on the shared forwarder instead of terminating and reaping a child process.

## Impact

- **Code:**
  - `local_service_network.py`: the `linux-bridge-socat` strategy becomes a leased shared forwarder. The probe accepts an address and port held by our live forwarder with a matching upstream. The PDEATHSIG `preexec_fn` hook is removed.
  - `egress_relay.py`: an optional peer filter, an IPv6 loopback target, and exact-port binding.
  - A new module holds the lease and attach logic (`ctl.lock`, `lease`, `upstream`).
  - `cli.py`: the start and close ordering. The "spawn before the relay thread exists" constraint goes away.
  - `doctor.py`: the `socat` check is removed.
- **State:** `$XDG_STATE_HOME/project-sandbox/local-forward/<gateway>-<port>/`. It is per user and private (directories `0700`, files `0600`).
- **Security:**
  - Exposure is unchanged. The listener is reachable from every container on any Docker bridge, as `socat` was.
  - Its lifetime now spans the union of the sessions that use it, plus about one poll interval, instead of a single session.
  - `docs/security.md` updates its listener comparison table.
- **Docs:** `docs/runtime.md`, `docs/usage.md`, `docs/doctor.md`, `docs/security.md`, `docs/egress-sidecar.md`, `docs/apple-container-dns.md` and `docs/development.md` (e2e cleanup) stop describing a per-session `socat`.
- **Tests:**
  - concurrent leases on one forwarder;
  - forwarder exit after the last release;
  - lease release by a killed session;
  - upstream mismatch refused;
  - attach during forwarder exit;
  - a foreign listener still refused;
  - the e2e cleanup helper updated for the new process.
- **Out of scope:**
  - Rootless Podman, which uses pasta or `host-gateway` and needs no bridge listener.
  - Rootful Podman, which is refused.
  - Apple `container` and Docker Desktop, which use native aliases.
  - Image builds, which do not use local-service forwarding plans.
  - Restricting which containers can reach the gateway listener, for example with a per-session Docker network.
