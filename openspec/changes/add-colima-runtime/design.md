# Design

## Context

See `proposal.md` for motivation. Today the sidecar is a single container per runtime that publishes Squid's port on one host address (`Strategy` in `egress_sidecar.py`: loopback, Docker gateway, or pasta). `sandbox_plan()` turns that endpoint into a `ForwardingPlan` that maps `host.docker.internal`, and the sandbox firewall admits that address and port. `Runtime` is a frozen dataclass of `name` and `binary`; 34 call sites across `container_cli.py`, `cli.py`, `doctor.py`, `egress_sidecar.py` and `local_service_network.py` build argv as `[runtime.binary, ...]`. Sidecar state lives at `egress/<runtime>/`.

Colima's Docker engine runs inside a Linux VM, so the sidecar and the sandboxes share one engine and one kernel. Docker's embedded networking can connect them without any host involvement.

## Goals / Non-Goals

**Goals:**
- A sandbox reaches Squid over a bridge inside the VM, with no host-published port, no `host.docker.internal` mapping and no host-side relay.
- No route to the Internet from a sidecar-mode sandbox except through Squid, enforced by network topology as well as the sandbox firewall.
- Minimal moving parts: network setup happens at sidecar creation, not per session.
- Other runtimes' code paths and behavior stay identical.

**Non-Goals:**
- Sandbox-to-sandbox isolation. All sandboxes of a profile share `psbx-sandbox`.
- Managing the VM (create, start, reset, mounts). Safe handling of host-reachable services (Ollama, agent gateways, loopback parents).
- A general network-plugin abstraction. This adds one strategy next to the existing ones.

## Decisions

### D1. Colima is a `Runtime` value with a context prefix, not a new binary

`Runtime` gains an optional `context: str | None`. Colima is `Runtime("colima", "docker", context="colima-<profile>")`, built by a factory from the profile name. A single helper `cli_argv(runtime, *args)` returns `[binary, "--context", context, *args]` when a context is set, and the existing argv otherwise. The 34 `[runtime.binary, ...]` call sites move to this helper; behavior for other runtimes is byte-identical (tested by comparing argv).

`runtime.name` stays `"colima"` for identity checks (`runtime.name == DOCKER.name` branches must not accidentally match Colima), and layout keys use `colima-<profile>`.

*Alternatives:* set `DOCKER_CONTEXT` in the subprocess environment (rejected: ambient state that is easy to lose across a `subprocess` call and invisible in dry-run output); `docker context use` (rejected: mutates the user's global state).

### D2. Verify, don't manage, the VM

Preflight runs `docker context inspect colima-<profile>`, then `docker --context ... info`, and checks the endpoint of the context is the Colima socket for that profile. Failure messages carry the `colima start --profile NAME` command. `colima` itself is invoked only if a probe needs it, and read-only (`colima status`/`list`); `colima` missing from PATH is not an error if the context verifies.

*Alternative:* `colima init`/lifecycle commands (deferred to a later change; they are a separate UX and security surface).

### D3. Two persistent networks; sidecar attached once at creation

```text
psbx-egress-uplink (bridge)             psbx-sandbox (--internal bridge)
        │                                      │
        └──────────► [ project-sandbox-egress ] ◄──────────┘   alias: egress
                      docker run --network psbx-egress-uplink
                      docker network connect --alias egress psbx-sandbox <sidecar>
                                               ▲
                                  sandbox A, B ┘  --network psbx-sandbox
```

The sidecar is created on the uplink and connected to the internal network (alias `egress`) as part of `ensure()`, under the existing sidecar lock, before readiness is reported. Sandboxes attach to `psbx-sandbox` only. Because attachment happens when the container is created, restart and drift handling reuse the existing recreate flow with no per-session attach/detach, no reconcile loop and a constant number of Docker networks.

Both networks carry ownership labels (`project-sandbox.owner=...`, `project-sandbox.profile=<profile>`, role). The CLI inspects labels before using a network and refuses a same-named network without them. Nothing is pruned in bulk. `project-sandbox-egress down` keeps both networks: they cost nothing while idle, and removing them would need every sandbox detached first. The docs list the `docker network rm` commands for a manual cleanup.

`--internal` is retained even though a single shared bridge would work, because it makes a bypassing process (one that ignores proxy variables) route-less, which matters in corporate setups where the sandbox firewall cannot be the only barrier.

*Alternative:* per-session internal networks (the full spec in `project-sandbox-colima-simplified-spec.md`). Rejected for the MVP: it adds attach/detach, reconcile after a sidecar restart, address-pool pressure and a bigger test matrix for peer isolation that is out of scope.

### D4. Pin `egress` into the sandbox; keep the firewall DNS-free

Sandbox firewalls today admit no DNS in sidecar mode. Docker's embedded DNS (127.0.0.11) would need a rule and would also answer for other sandboxes' names. Instead, at launch the CLI reads the sidecar's IPv4 address on `psbx-sandbox` (`docker inspect`), passes `--add-host egress:<ip>`, and gives the firewall that single address on port 3128. Proxy variables use `http://egress:3128`. If the sidecar is recreated its address may change, and a launch after that reads the new one; sandboxes already running keep the old address and fail closed. That already matches the stated behavior of sidecar restarts for other runtimes.

*Alternative:* Docker DNS plus a firewall rule for 127.0.0.11:53 (rejected: widens the sandbox's trust surface). *Alternative:* fixed subnet and static IP (deferred; it would avoid inspect, but a chosen subnet can collide with corporate VPN ranges, which is a known pain point).

### D5. A new strategy kind, selected by the runtime

`Strategy` gains kind `DOCKER_PRIVATE_NETWORK`, returned by `endpoint_strategy()` when `runtime.name == "colima"`. Code that assumes a published port branches on the strategy: `sidecar_run_argv` omits `--publish` and adds `--network`; `port_free`/`probe_published` are skipped; readiness uses the in-container probe (`exec`) only; `ContainerInfo.port/address` labels hold the fixed port 3128 and the internal address; `sandbox_plan()` returns a plan describing `--network psbx-sandbox`, `--add-host`, and the firewall endpoint instead of a `host.docker.internal` mapping. `recorded_strategy` learns the new kind so drift checks keep working. The `[egress.sidecar] port` check rejects non-default values on Colima (see the "proxy port is fixed" requirement), and the port is excluded from the Colima restart fingerprint.

### D6. Host-dependent features fail early

An allow-list of features is not needed; the CLI computes whether the launch needs a host path (Ollama enabled, `--agent-proxy` with a local target, `[egress.upstream]` host loopback) and fails with a feature-specific message before any container work, in the same place that sidecar-mode combination errors are produced today. A parent proxy reachable by name or address is passed through unchanged: Squid egresses through the uplink and the VM's NAT. The Docker Engine parent forwarder and Apple relay are never started for Colima.

### D7. Profile-scoped state

`layout()` keys the state directory by `colima-<profile>` (`egress/colima-project-sandbox/`). The sidecar container, CA, locks and networks are per profile: container name and network names are fixed within the profile's engine, so they cannot collide across profiles, and the on-disk layout still keeps profiles apart. Image caches already live in each profile's engine.

### D8. Mount visibility is checked, not configured

Docker can bind-mount only what the VM exports. The preflight starts no new container: it runs a throwaway `docker run --rm -v <path>:<path>:ro <image> test -e <path>` for the parent directories of each mount source (deduplicated, one container per launch), and reports missing paths. This avoids guessing at Colima's mount configuration, which differs between VZ and QEMU and across versions.

## Risks / Trade-offs

- **[Internal network behaves differently from expectation (alias, DNS, Squid reachability)]** → Spike 1 verifies alias resolution and connectivity on an `--internal` network before any code depends on it.
- **[Squid acts as an IP router between networks]** → Spike 2 checks forwarding inside the sidecar and tests direct access to the gateway, uplink address, IPv6 and UDP from a sandbox; failures block the change.
- **[Sandboxes can reach one another on `psbx-sandbox`]** → Accepted for the MVP and documented. The shared network is trusted within the profile's VM.
- **[Build-time proxy/CA do not work behind the corporate setup]** → Spike 3 builds a sidecar image through an intercepting proxy in Colima, reusing recent build-proxy work.
- **[Mounts differ between VZ and QEMU, symlinks and `.git` for `--branch`]** → Spike 4 covers workspace, staging, credential files and a worktree's `.git`.
- **[Sidecar address changes after recreate]** → Running sandboxes fail closed (they hold the old address); new launches read the new one. Documented.
- **[Corporate security software still interferes with the VM's own outbound path]** → Out of reach of this change. The design removes only the sandbox-to-sidecar hop from that surface.
- **[Argv refactor touches 34 call sites]** → Do it first, as a no-behavior-change step with an argv-equality test over the other runtimes.

## Migration Plan

Additive: new runtime and strategy; no change for existing runtimes, configuration or state. Rollback is to stop using `--runtime colima`; leftover VM-side networks and the `project-sandbox-egress` container can be removed with `docker --context colima-<profile> rm`/`network rm`, which the docs list.
