# Design

## Context

`add-squid-egress-sidecar` (archived 2026-11-06) built the sidecar as a per-session resource. `egress_sidecar.session()` is a context manager that, for one session:

- creates `egress/<sid>/` with a flock, a fresh CA, `squid.conf` and `upstream-ca.pem`;
- creates the network `sbx-egress-<sid>`;
- runs `project-sandbox-egress-<sid>`, passing the forwarders as `SBX_FORWARD_PORTS` and the subnet as `SBX_INTERNAL_SUBNET`;
- waits for readiness;
- writes `sandbox/egress.conf`;
- tears everything down on exit.

The Apple parent-proxy relay is a `BuildProxyForwarder` thread inside the CLI process. The session-end summary parses the whole `logs` output. `cleanup_orphans()` removes labelled resources whose session lock is free.

Facts the design relies on:

- Apple `container` cannot attach a network to a running container. The sidecar must therefore join the internal network at `run`, which forces one shared network.
- The sidecar entrypoint copies `squid.conf` from the read-only input mount to `/etc/squid/` and runs `squid -N` as PID 1. A reload is therefore copy plus `squid -k reconfigure`.
- `container_cli.select_runtime()` already chooses the default runtime. `local_service_network` already detects Docker Desktop (`_docker_is_desktop`) and pasta (`_podman_uses_local_pasta`).
- `egress_policy.parse()` rejects unknown keys. Today it knows only `allow` and `copilot.enabled`.

## Goals / Non-Goals

**Goals:**
- Starting and reusing the sidecar is idempotent and serialized, so two launches can never create two sidecars.
- Allowlist and forwarder changes never need a restart.
- Everything the sidecar owns can be described from host state plus one `inspect`, without tracking sandboxes.

**Non-Goals:**
- Pinning the sidecar's IP so that sandboxes survive `restart`. A user-initiated restart breaks running sandboxes, as the user accepted.
- Keeping a sidecar-mode sandbox working across a sidecar crash.
- Different policies for different sandboxes on one runtime.
- Linux forwarding (see `global-egress-sidecar-linux`) and devcontainers.

## Decisions

### 1. Names and host state

Per runtime there is one container `project-sandbox-egress` on one network `sbx-egress`. Both carry the label `project-sandbox.egress.role=sidecar|network`, and the container also carries `project-sandbox.egress.fingerprint=<restart fingerprint>` (Decision 3). Runtimes have separate namespaces, so the fixed names never collide.

Host state lives in `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/` (0700):

```
egress/<runtime>/
├─ .lock                 serializes ensure / up / down / restart / reload / edits
├─ sidecar/              mounted read-only into the sidecar at /project-sandbox-egress
│  ├─ squid.conf  forwarders  upstream-ca.pem
│  └─ ca.pem  ca-key.pem (0600)
├─ state.json            image tag, fingerprints, subnet, sidecar IP, CA not_after, relay port, started_at
├─ relay.lock relay.pid relay.log
└─ sessions/<sid>/       per-session sandbox inputs (Decision 8), mounted read-only into that sandbox
```

`state.json` is a cache, not the source of truth. Every decision re-checks the container with `inspect`. A missing or unreadable `state.json` triggers a fresh `up`.

*Alternative:* a fingerprint-named sidecar per configuration, so several can run side by side. Rejected during exploration: it brings back per-configuration networks, accumulates idle VMs on Apple, and does not fit "one control point".

### 2. Ensure, under one lock

Launches and `up` share one function:

```
with path_lock("egress-sidecar", state_dir):
    remove legacy per-session resources whose session lock is free
    c = inspect(project-sandbox-egress)
    if c is None or not running(c) or not ready(c) or sidecar/ incomplete:
        rm -f container                               # also clears a stopped one after a reboot
        network: reuse sbx-egress if present, else create --internal (Podman: --disable-dns)
        CA: keep if valid >= 2 days and constraints match, else regenerate (Decision 7)
        render squid.conf, forwarders, upstream CA; write state.json
        run sidecar; on Docker/Podman: network connect; read IP; wait_ready
    relay: if configured and relay.lock is free → spawn (Decision 6)
    drift: reload-only differences → reload; restart-only differences → warning (Decision 3)
    CA warnings / refusal (Decision 7)
```

Only the launch path continues after the lock: it writes `sessions/<sid>/` and runs the sandbox. Holding the lock during a cold start (seconds) blocks a second launch until the sidecar is ready, which is exactly what that launch needs.

`down` takes the same lock, then:
1. kills the relay;
2. runs `rm -f` on the container;
3. runs `network rm`, ignoring the failure when sandboxes are still attached;
4. deletes `sidecar/` (including the key) and `state.json`.

`restart` is `down` without the CA deletion, followed by `up`, with the CA kept according to Decision 7.

### 3. Two fingerprints: reloadable and restart-only

The launch must not restart the sidecar implicitly, because that would break other users' sandboxes. Desired state is therefore split into two fingerprints:

| Fingerprint | Inputs | When it differs |
|---|---|---|
| reload | rendered `squid.conf` (allowlist and providers) | reload live |
| restart | sidecar image tag, Copilot flag (CA constraints), upstream URL and CA bytes, memory/CPU, log options | warn and keep using the running sidecar |

Forwarders are neither: they are reconciled live (Decision 5).

The upstream changes `cache_peer` in `squid.conf` too. Because it is listed under restart, a changed upstream never reloads into Squid without its relay. To keep the two fingerprints from overlapping, the reload fingerprint is computed over `squid.conf` rendered with the *running* upstream.

### 4. Live reload

The host writes the new `squid.conf` to `sidecar/squid.conf.tmp` and renames it over the old file. The directory is mounted, not the file, so the rename is visible inside the sidecar. The host then runs:

```
<runtime> exec project-sandbox-egress sbx-reload
# sbx-reload (in the image): cp /project-sandbox-egress/squid.conf /etc/squid/squid.conf
#                            squid -k parse && su-exec squid squid -k reconfigure
```

`squid -k parse` first means a bad config never reaches the running Squid; the host already validated it, so this is belt and braces. Squid keeps established tunnels across `reconfigure`. Spike 0.3 confirmed this on the pinned Squid 6.12 (see Spike results). `squid -k reconfigure` finds the running Squid through `pid_filename /run/egress-sidecar/squid.pid`; the earlier `pid_filename none` would have made it fail.

*Alternative:* restart on every allowlist change. Rejected because it breaks every running sandbox.

### 5. Forwarders are global, firewall wiring is per session

The forwarder list moves from `SBX_FORWARD_PORTS` to the file `sidecar/forwarders` (one port per line), so it can change without a new container.

`sbx-forwarders` in the image reconciles running socat processes with that file:
- it starts missing ones with `start-stop-daemon --background` as `nobody`, using `range=<subnet>` as today;
- it stops removed ones by their pid file.

The entrypoint runs it once at start, and `forward add|remove` runs it through `exec`. Spike 0.2 checks that a process started from `exec` survives the end of the `exec` session on Apple `container`, Docker Desktop and Podman machine.

*Implementation note (spike 0.2):* the Alpine base has no `start-stop-daemon` (BusyBox is built without it, and OpenRC's has different options). `sbx-forwarders` therefore starts each forwarder as `su-exec nobody setsid socat …` in the background and records its pid. A pid file is trusted only while `/proc/<pid>/cmdline` is that port's `socat`, so a stale or reused pid is never signalled. `sbx-forwarders --list` prints the live ports for `status` and `forward list`.

Each session's `egress.conf` lists only the ports that its `_local_services()` needs. A needed port that is missing from the running forwarders fails the launch with `project-sandbox-egress forward add <port>`.

On Docker Engine and rootless Podman with pasta, `up` with a non-empty forwarder list fails, using the existing runtime probes. Without forwarders the sidecar works there.

### 6. Relay as a detached, sidecar-owned process

A new entry point, `python -m project_sandbox.egress_relay --state-dir … --port P`, runs `BuildProxyForwarder` in the foreground:
- it holds an exclusive flock on `relay.lock` for its whole life;
- it writes `relay.pid`, and `relay.port` with the port it actually bound (the requested one, or a free one when that is taken);
- it logs to `relay.log`, truncated at start.

It is spawned with `start_new_session=True` and stdin, stdout and stderr detached. Terminal signals and the CLI's `_exit_on_termination_signals` therefore do not reach it.

- **Liveness check** is "`relay.lock` is held", the same pattern as `session_ca.is_live`, which avoids trusting a pid that may have been reused.
- **`down`** sends SIGTERM to `relay.pid` only while the lock is held, then waits for the lock to be released.
- **Respawn** uses the recorded port.
- **Port move:** if the recorded port is no longer free, the relay binds port 0. Its new port goes into `state.json`, `squid.conf` is re-rendered and reloaded (Decision 4), and the restart fingerprint is unaffected.

The relay is needed only on Apple with a remote upstream. Docker and Podman dial the parent directly, and a loopback upstream goes through `host.docker.internal`.

*Alternative:* a relay in every sandbox's CLI process, with takeover of a fixed port. Rejected: in-flight tunnels drop whenever the owning sandbox exits. *Alternative:* a launchd agent. Rejected as unnecessary, since a launch respawns the relay anyway.

### 7. The CA belongs to the sidecar

`session_ca.generate()` keeps its parameters and gains a lifetime from `[egress.ca] lifetime_days`. The default is 7 days and the allowed range is 1 to 30; `MAX_LIFETIME` becomes 30 days. The common name is `project-sandbox egress <runtime>`.

The CA is reused when it is valid for at least 2 more days and its constraints match the Copilot setting. Otherwise `up` and `restart` regenerate it, and so does `restart --new-ca`.

When interception is active (the Copilot profile):
- a launch warns when the CA has less than 2 days left;
- a launch refuses when the CA has expired.

With nothing intercepted the CA is constrained to `invalid` and never trusted by sandboxes, so expiry is irrelevant and launches stay silent about it. `down` deletes the key.

### 8. Per-session sandbox inputs

`sessions/<sid>/` reuses `session_ca.session_directory()` (flock while the session runs, `remove_stale()` for crashed sessions). It contains:
- `egress.conf`, with `sidecar_ip` and this session's `forward_ports`;
- `ca.pem` when interception is active;
- `upstream-ca.pem` when an upstream CA is configured.

The sandbox firewall step installs both certificates into the system trust, which it already does for `ca.pem`. Sandbox images therefore no longer depend on the upstream CA, which is why `--ca-cert` is rejected in sidecar mode. The key is never placed here.

### 9. Policy file and editing

`egress.toml` gains these keys:

```toml
[egress]
allow = [...]
[egress.copilot]   enabled = false
[egress.upstream]  url = "..."          ca_cert = ["~/corp.pem"]   # paths, read at render time
[egress.ca]        lifetime_days = 7
[egress.sidecar]   memory = "256M"      cpus = 1     forward_ports = [8787, 11434]
```

- **Validation:** the upstream URL uses `proxy_endpoint` validation (the same rules as `--internet-proxy`). Forward ports use `validate_forward_ports`.
- **Allowlist:** the effective allowlist is `allow` plus the union of every agent's provider hosts.
- **Mount check:** the existing check that keeps the policy outside the sandbox's reach now runs against the fixed `egress.toml` path and the state directory, against every mount source of the launch.
- **Edits:** `allow` and `forward` edits use **tomlkit** (new pinned dependency, pure Python) so comments and unknown-but-valid formatting survive. Reading stays with `tomllib`, and every edit is re-validated through `egress_policy.parse()` before the file is replaced atomically.

*Alternative:* append-only edits without tomlkit. Rejected because `remove` cannot be done that way.

### 10. Launch changes

- `--egress-policy` is removed from the parser.
- In sidecar mode, `_validate_egress_args` rejects `--extra-domain`, `--internet-proxy` and `--ca-cert`, naming the replacement.
- `--agent copilot` checks the configured or running Copilot setting.
- `_start_egress_sidecar` becomes ensure plus session inputs. The `ExitStack` holds only the `sessions/<sid>/` directory, with no sidecar teardown.
- The Apple relay in the CLI process is no longer created in sidecar mode.
- `--dry-run` runs `inspect` read-only when the runtime is reachable. It reports "would start" or "would reuse", plus drift, and creates nothing.

### 11. Logs and the denial count

- `squid.conf.j2`'s `sbx` logformat gains `client=%>a`.
- On Docker and Podman, the sidecar runs with `--log-opt max-size=10m` (Docker also gets `max-file=3`).
- At session start and end, the CLI reads the sidecar's clock (`exec date +%s`), because VM clocks can drift from the host's after sleep. At the end it counts classified denials (`egress_log.classify`) whose `%ts` falls in that window.
  - Docker and Podman narrow the fetch with `logs --since`.
  - Apple fetches the whole log: `container logs` has no `--since` (only `-n` and `--follow`), and spike 0.4 measured 0.02–0.04 s for 50k lines, so the fallback in Risks is not needed. Apple's runtime log is not size-capped.
- `project-sandbox-egress log` reuses the same parser for `--denied`, `--since` and `--follow` (passed through to `logs --follow`).

### 12. Resource defaults

The sidecar runs with `--memory 256M --cpus 1` by default on every runtime, Apple included (spike 0.1). The rendered `squid.conf` bounds Squid's memory with `cache_mem 8 MB` and `max_filedescriptors 8192`. Without them, Squid sizes its fd table from the runtime's `nofile` ulimit (about 1M on Docker Desktop), which kept it at 166 MB resident and a 186 MB peak, too close to the 256 MB limit.

### 13. The `project-sandbox-egress` executable

- **Module and entry:** `project_sandbox/egress_cli.py` with argparse subparsers, registered under `[project.scripts]`.
- **Runtime selection:** `--runtime` uses `container_cli.select_runtime()`.
- **Exit codes:** 0 for success, 1 for operational failure, 2 for usage.
- **`status`:** text, or JSON with `--json`.
- **Other tools:** `project-sandbox doctor` reports the global sidecar's status line and lists legacy per-session resources. Doctor is read-only by contract, so it names `project-sandbox-egress up`, which removes them (as do sidecar launches and `down`).

## Risks / Trade-offs

- **[All sidecar sandboxes share one L2 network]** → The sandbox firewall drops everything except TCP to the sidecar (`INPUT` and `OUTPUT` default DROP), the agent has no root, and Squid refuses IP literals and private resolutions in direct mode. `docs/security.md` states that isolation between sandboxes rests on these checks, and points to firewall mode for stricter isolation.
- **[The CA key lives up to 30 days on the host]** → Mode 0600 in a 0700 directory, never mounted into a sandbox (checked at every launch), name-constrained to `github.com` or `invalid`, and deleted by `down`.
- **[`down`/`restart` break running sandboxes]** → Accepted by design. `status` and `down` print the attached containers first, so the user sees what will break.
- **[A hand edit of `egress.toml` widens the policy for running sandboxes at the next launch]** → Intended (one control point). The launch prints what it reloaded.
- **[`reconfigure` might drop long-lived tunnels]** → Spike 0.3. If it does, live reload is limited to `allow add`/`reload` run explicitly by the user, and launches warn instead of reloading.
- **[A relay process left behind]** → It holds `relay.lock`, so `status` shows it and `down` kills it. Even without them it is harmless: it serves only loopback and dies at logout.
- **[The launch keeps running on restart-only drift]** → The warning names each field. The alternative, an implicit restart, would break other sessions.
- **[A long Apple log fetch at session end]** → Spike 0.4. The fallback is to print the count only when the fetch is fast enough, and otherwise print the `log` command alone.

## Migration Plan

1. Ship together:
   - removal of `--egress-policy`;
   - the new rejections;
   - the new executable;
   - the documentation of the three egress options (firewall, sidecar, your own proxy through `--internet-proxy`) in `docs/egress-sidecar.md` and `docs/internet-proxy.md`.
2. Existing `egress.toml` files stay valid: the new keys are optional.
3. Users of `--egress-policy` move the file to `$XDG_CONFIG_HOME/project-sandbox/egress.toml`. Users of `--internet-proxy`/`--ca-cert` in sidecar mode move them to `[egress.upstream]`. The rejection messages say exactly this.
4. Legacy per-session resources are removed by the first `up`, launch or `doctor`.
5. Rollback: an older version ignores the global sidecar. Run `project-sandbox-egress down` before downgrading, or remove `project-sandbox-egress` and `sbx-egress` by hand.

## Open Questions

- The exact defaults for `--log-opt` sizes, and the warning wording, can be tuned during implementation without changing the specs.

## Spike results (2026-11-06)

Apple `container` 1.4.1 and Docker Desktop 29.9.2 on macOS, sidecar image built from this change (Squid 6.12 on Alpine 3.22.1). Spike 0.2 was repeated later the same day on a rootless Podman machine (Podman 6.0.1, applehv, Fedora 44, netavark).

- **0.1 Resources.** With `--memory 256M --cpus 1`, the sidecar became ready on both runtimes, and ten concurrent HTTPS requests through it to `pypi.org` all returned 200 (0.07–0.19 s each). Before `cache_mem`/`max_filedescriptors` were bounded, Docker showed Squid at 166 MB RSS with a 186 MB cgroup `memory.peak`. Afterwards, `memory.peak` was 25 MB on Docker and 37 MB on Apple (the Apple VM reports 370 MB total). **Apple default: 256M**, the same as the other runtimes.
- **0.2 Detached forwarders.** `project-sandbox-egress forward add 18999` ran `sbx-forwarders` through `exec`. Three seconds after the `exec` returned, `socat` was still listening on `0.0.0.0:18999` as `nobody` on Docker Desktop and on Apple `container`, and `forward list` reported it as running. `forward remove` stopped it on both. On Podman machine, `socat` was still listening as `nobody` (with `range=` set to the internal subnet) 16 s after the `exec` returned; `forward list` reported it as running, and `forward remove` stopped it. `start-stop-daemon` does not exist in the image; see the Decision 5 implementation note.
- **0.3 Reconfigure with live tunnels.** A client on `sbx-egress` streamed a 50 MB download from `speed.cloudflare.com` through a CONNECT tunnel at about 0.7 MB/s for 60 s. Meanwhile `allow add` and `allow remove` ran `sbx-reload` (`squid -k parse`, then `squid -k reconfigure`) twice on Docker and once on Apple. The stream survived without interruption on both (42.8 MB in 60 s), and a host added live was tunnelled (`TCP_TUNNEL/200`) right after the reload. The Decision 4 risk did not materialise; launches reload live as designed.
- **0.4 Apple logs.** `container logs` supports only `-n <lines>` and `--follow`. Fetching a 50k-line log took 0.02–0.04 s, so the session-end count fetches the whole log and filters by `%ts`.
- **0.5 Detached relay.** Covered by the `parent` check of the `egress-sidecar` e2e suite on Apple: session 1 runs in its own process group, which is hung up after it exits (as a closed terminal would), and the relay must still hold its lock and serve session 2. Passed on Apple `container` 1.4.1 (`parent:relay-survives-first-session`, and session 2 reached the fixture parent through the relay); see `docs/development.md`.
