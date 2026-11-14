# RFC: Colima backend with a managed VM and private in-VM egress

**Status:** Proposed; technical spikes required before implementation  
**Date:** 2026-11-14  
**Target:** macOS 13+; Apple Silicon and Intel where supported; Colima with Docker Engine  
**Repository:** https://github.com/pkrusche/project-sandbox  
**Supersedes:** `project-sandbox-colima-managed-vm-spec.md` (simplifies the egress networking design)

## 1. Decision summary

Implement `--runtime colima` using **one dedicated project-sandbox-owned Colima profile** (default name `project-sandbox`). Run sandbox containers and the existing Squid egress sidecar under the Docker Engine **inside that VM**. For `--egress sidecar`, sandbox traffic reaches Squid through a private Docker network and Docker DNS name (`egress:3128`). **Do not publish Squid on a macOS port, the VM's Docker bridge gateway, or `host.docker.internal`.**

The default network topology should isolate **individual sandbox sessions**, not just the managed VM: create one Docker `--internal` bridge per session, attach the shared Squid container to it with alias `egress`, and attach only that session's sandbox container to the network. Squid also connects to a separate, ordinary outbound Docker bridge for upstream traffic. This adds small, explicit network-attach/cleanup operations but avoids relying on the sandbox's own firewall to prevent one sandbox from reaching another.

The **macOS host-service problem is separate**. Ollama, agent gateways, macOS-loopback upstream proxies, and build-time proxy endpoints may still require a verified VM-to-host path or a narrow relay. Do not solve those by publishing the *egress* proxy back to macOS or by granting the sandbox general outbound networking.

Keep Apple `container` first in `--runtime auto`; retain generic Docker/Podman behavior unchanged. Target **functional** feature parity, not per-container VM isolation parity.

### Removed from the previous design

- No Colima-specific host-published Squid port or Docker bridge gateway probing for egress.
- No macOS egress relay or per-sandbox forwarding process merely to contact Squid.
- No host-port availability checks or port-collision recovery for Colima's egress sidecar.
- No egress-related `host.docker.internal:host-gateway` injection.
- No global Docker context switching, user-owned profile mutation, or implicit Colima VM creation.

## 2. Architecture

```text
macOS
  project-sandbox CLI ── docker --context colima-project-sandbox ──┐
  local Ollama / gateways / optional upstream proxy                 │
                      (separate, narrowly scoped integration)      │
                                                                    ▼
         dedicated Colima profile: project-sandbox (Linux VM)
         ┌──────────────── Docker Engine ───────────────────────────┐
         │                                                         │
         │  internal network: psbx-session-<A>                      │
         │  [Sandbox A] ──HTTP(S)_PROXY──> [egress alias] ──┐       │
         │                                                 │       │
         │  internal network: psbx-session-<B>              │       │
         │  [Sandbox B] ──HTTP(S)_PROXY──> [egress alias] ──┤       │
         │                                                 ▼       │
         │                                      [one Squid sidecar] │
         │                                                 │       │
         │                                      [psbx-egress-uplink] │
         │                                                 │       │
         └─────────────────────────────────────────────────┼───────┘
                                                           ▼
                                                Internet / parent proxy
```

**Trust boundaries:** Other Colima profiles have separate VMs and Docker engines. Each sandbox session gets an internal Docker network, with no normal route to the Internet. The trusted Squid sidecar is connected to the session networks and an outbound network; sandbox containers are **never** connected to the outbound network in sidecar mode. A compromised sidecar or a macOS process with access to the managed Docker socket is still inside the trusted boundary; multiple sessions share the VM kernel. The VM itself must not export unrelated macOS home directories.

### Network allocation and lifecycle

| Object | Scope | Rules |
|---|---|---|
| Colima profile `project-sandbox` | Persistent, managed | Separate VM, Docker engine, images and volumes; validated ownership |
| `psbx-egress-uplink` | Persistent per managed profile | Ordinary bridge, used by Squid for Internet/upstream; **never join sandbox containers** |
| `psbx-session-<id>` | One sandbox session | User-defined `--internal` bridge; only its sandbox, the shared Squid sidecar, and explicitly required trusted host-service relay(s) |
| `project-sandbox-egress` | One sidecar per Colima profile | Docker network alias `egress` on each active session bridge; internal Squid port `3128`; **no `-p` / `--publish`** |
| Host-service relay | Only when enabled | A distinct, narrowly scoped component; not part of ordinary egress traffic |

Use managed Docker labels for ownership, session ID, role and network purpose. Generate unpredictable session IDs; never interpret arbitrary user-provided names as managed resources. Inspect and validate labels before attaching/removing a network, container or sidecar. Keep per-profile locks around sidecar network attachment/recreation and cleanup.

**Shared sidecar detail:** The sidecar begins attached to `psbx-egress-uplink`. When session A starts, create `psbx-session-A`, `docker network connect --alias egress psbx-session-A project-sandbox-egress`, verify that the alias resolves from that network, and only then start Sandbox A. On session exit, remove Sandbox A, disconnect Squid from its session network, and remove the network. Do not restart the sidecar merely because a session starts or ends. A deliberate sidecar restart may interrupt live sessions and must respect existing lifecycle warnings/locks. After an unexpected restart, refuse new launches until network attachment and readiness are reconciled; fail existing sessions closed.

**Port semantics:** The existing Squid **container** listener is `3128` (`squid_config.LISTEN_PORT`). The existing `[egress.sidecar] port` config currently controls a *published host port*. It is not used in Colima private-network mode. Keep the setting for other runtimes; for Colima, use `egress:3128` and **error on an explicitly non-default sidecar port** rather than silently ignoring the user's value. Document this deliberate difference.

## 3. Runtime selection and managed VM lifecycle

- Add `--runtime colima`; managed profile `project-sandbox` is the default. `--colima-profile NAME` is an explicit, **unmanaged** advanced override; it neither authorizes project-sandbox to create/delete that profile nor guarantees isolation from other workloads in that profile.
- One-time explicit `project-sandbox colima init --workspace-root PATH` (or `project-sandbox-colima init` if the existing positional CLI makes subcommands impractical). Init uses a restrictive profile config, Docker runtime, controlled mounts, no default writable `$HOME` export, and no SSH-agent forwarding. A stopped, verified owned profile may be started automatically for subsequent runs.
- Managed lifecycle commands: `status`, `start`, `stop`, `reset` (reset destructive and confirmation-protected). Never auto-reset/delete. Reject profile-name collisions, configuration drift and unfamiliar Docker contexts/sockets; use cross-process locks and active-session guards.
- Every Docker operation goes through an explicit verified `docker --context colima-project-sandbox ...` (or validated equivalent socket), including image builds, network operations, sidecar management, logs and cleanup. Colima startup must **not** activate/change the user's global Docker context; validate `--activate=false` with real versions.
- Keep persistent image/volume caches inside the managed VM and scope cache identity, state and locks by profile. `doctor`, `status` and `--dry-run` remain non-mutating. Preserve Apple `container` auto precedence.
- VM-level mounts allow only approved workspace roots plus private app configuration/staging roots actually needed. A Docker container bind mount cannot expose a macOS directory that Colima did not export; preflight every generated and user-added mount and reject a missing path. VM-root can access **all** exported roots, so do not export an entire home directory.

## 4. Egress implementation

### 4.1 Sidecar mode (new Colima-specific network strategy)

1. Confirm the managed Docker Engine, required image and egress policy are valid.
2. Ensure the owned outbound bridge and healthy Squid sidecar exist; Squid has outbound connectivity only through that bridge, and its listener is **not published**.
3. Create a session-only internal bridge and connect Squid to it as `egress`. Resolve/test the alias from a container attached to the **same** network. Probe both Squid's internal readiness and the expected allow/deny behavior through the network, not through a macOS TCP socket.
4. Launch the sandbox on **only that internal bridge**. Set `HTTP_PROXY`, `HTTPS_PROXY` (and lowercase equivalents where existing behavior requires them) to `http://egress:3128`. Preserve existing CA provisioning, Squid allowlists, TLS/SNI checks, logs and denial reporting.
5. Preserve the existing in-container firewall as defense in depth: allow only the resolved proxy address/port, loopback/required internal DNS, and explicitly authorized host-service relays. The network topology must also prevent direct Internet bypass if the sandbox process is misconfigured to ignore proxy variables. Re-check after Squid restarts or address changes; stale endpoints must fail closed.
6. On teardown, disconnect only this session's network attachment and remove only its labelled network. Clean up abandoned resources after a crash using ownership labels and active-session verification, never a broad `docker network prune`.

Docker `--internal` networks are not a complete substitute for auditing guest-gateway services or local peer access. Test direct access to the bridge gateway, container address literals, IPv4/IPv6, UDP, DNS and metadata targets. Do not grant agents Docker-socket access, privileged mode, or capabilities enabling them to rewrite networking. Also test that the dual-homed Squid container does not act as a generic IP router/NAT path around its HTTP policy.

### 4.2 Other egress modes

Keep `--egress firewall` as the current **direct** Docker networking mode, enforcing the project's existing iptables/ip6tables network policy. Do not put firewall-only sessions on an internal bridge, because that would break their allowed direct outbound connections. Preserve any explicit no-firewall mode and its documented reduced restrictions. The managed VM isolates these modes from other Colima profiles, but `firewall` and `none` are **not** protected by the sidecar-only topology.

### 4.3 Host-local services (independent concern)

A macOS service bound to `127.0.0.1` (Ollama, agent gateway, or a parent/build proxy) may not be directly reachable from the Linux VM or a container, depending on the Colima/Lima version and VM driver. Handle this with **one separate host-service adapter**, not through `egress_sidecar.endpoint_strategy()`:

- First test native VM-to-macOS loopback access using supported Lima/Colima host aliases. Do **not** assume Docker's `host-gateway` maps to macOS loopback; the Docker bridge gateway is inside Linux.
- If native access is insufficient, use an authenticated/owned, port-limited tunnel or a tightly scoped host-side relay. No wildcard macOS listener, no blanket host access, and no automatic firewall disable.
- For sidecar-mode sandboxes, any trusted host-service relay joins the **session's internal network** with a dedicated alias, is permitted only for the requested service and port, and has its own separately controlled path to macOS. The sandbox never joins `psbx-egress-uplink` to reach a host service.
- For a **macOS-local upstream proxy**, only Squid needs the host path; it connects from its trusted uplink side. No additional sandbox-side forwarding is required.
- Build-time proxy connectivity is a separate preflight because BuildKit/build containers may not use the same session network. Preserve existing build/proxy/CA behavior without granting runtime sandboxes unrestricted egress.
- If the host-service route cannot be made narrow and fail-closed, **block that feature on Colima** rather than opening up general networking. Report the precise unsupported service/mode.

### 4.4 Code changes focused on existing seams

| Area | Minimal design change |
|---|---|
| `container_cli.py` | Add Colima runtime identity, context-aware command generation and sidecar-vs-firewall network selection |
| New `colima_backend.py` | Owned-profile init, status/start/stop/reset, config/daemon verification, mount preflight |
| `egress_sidecar.py` | Add `docker-private-network` strategy; no `--publish`/host checks for Colima; attach/detach shared Squid to session internal networks; profile-scoped state |
| `local_service_network.py` | Only handle actual macOS host services; Colima-specific host-service adapter, not egress-sidecar discovery |
| `squid_config.py` | Keep fixed `LISTEN_PORT=3128`, existing policy, CA and helper behavior unchanged |
| `internet_proxy.py`, build-proxy integration | Resolve parent/build proxy reachability separately; do not assume local bridge gateway |
| `doctor.py`, test scripts, docs | Managed profile verification, explicit network mode and operational diagnostics |

Prefer small additions to existing abstractions rather than a generic network plugin framework. Do not refactor Apple/Docker/Podman egress networking as part of this change.

## 5. Technical spikes (run first)

Each spike produces reproducible commands, platform/Colima/Docker versions, observed output and a **PASS / WORKAROUND / BLOCKED** verdict in `docs/spikes/colima/`.

| Spike | Priority | Test and pass condition |
|---|---|---|
| **S1 — Managed profile ownership** | P0 | Two profiles with different Docker contexts/engines and state; explicit init; no global context changes; unowned collision, safe stop/reset and crash recovery |
| **S2 — Restrictive VM mounts** | P0 | No default home export; approved workspaces and private staging work; unrelated `~/.ssh`/repos invisible in VM; Git/jj/symlinks/credentials work on VZ/virtiofs and QEMU fallback |
| **S3 — Private sidecar topology** | P0 | Run a dual-homed Squid container on one uplink and **two per-session `--internal` bridges**; Docker alias `egress`, concurrent use, no published ports, port-free macOS; attach/detach/restart/reconcile tested |
| **S4 — Egress bypass/security** | **P0 release gate** | Sandbox can access only allowed targets via Squid; raw TCP/UDP, DNS, IPv6, direct bridge gateway, other sandbox, host metadata and sidecar-routing bypasses fail; Squid denial logs and failures behave correctly |
| **S5 — Host loopback adapter** | P0 for affected features | Ollama and local upstream/build proxies reachable without wildcard host listeners or broad sandbox access. Validate both VZ and QEMU and failure behavior; separate blocked features if no safe route |
| **S6 — Parity and concurrency** | P1 | Existing agents, image/build options, Git/jj, timeouts, sidecar commands, `--egress firewall`, no-firewall override, resource limits, devcontainers, profiles and VM restart work; test mixed concurrent egress modes |

**Implementation go/no-go:** S1–S4 must pass before sidecar mode ships. S5 is a gate for whichever host-service features are claimed supported; do not assert full feature parity until they pass. S6 must cover the current E2E matrix before release.

## 6. Ordered implementation tasks

| ID | Task | Dependencies | Done when |
|---|---|---|---|
| T01 | Run S1–S5 and record decisions | — | Managed VM, private-network and host-service feasibility proven or explicitly blocked |
| T02 | Implement Colima runtime and explicit Docker context | T01 | Every Docker invocation targets the verified profile; global context unchanged |
| T03 | Implement owned VM init/lifecycle and mount preflights | T01, T02 | Safe init/start/stop/reset; restricted exports; collision and active-session protections |
| T04 | Add managed network resource helper | S3, T02 | Create/inspect/label/attach/detach/remove session internal and persistent uplink bridges safely |
| T05 | Implement Colima `docker-private-network` sidecar strategy | T04 | Squid dual-homed, `egress:3128` alias, no host publishing or macOS proxy relay; policy and logs retained |
| T06 | Wire sandbox sidecar-mode network plan and readiness | T05 | Session has only private network; proxy env/CA/firewall correct; end-to-end allowed and denied probes |
| T07 | Implement separate Colima host-service adapter | S5, T04 | Ollama, gateway, parent/build proxy paths work safely; no broad host listeners or fallback to unrestricted egress |
| T08 | Keep other egress modes compatible | T02, T06 | `firewall` remains direct+filtered; `none` behavior unchanged; mode switching safe |
| T09 | Profile-scope images, sidecar metadata, locks and crash cleanup | T03–T08 | No cross-profile or cross-session deletion; parallel launch, restart and stale-resource tests pass |
| T10 | Security/regression tests | T06–T09 | S4 bypass suite automated, including peer isolation, guest gateway, DNS, IPv6 and sidecar routing |
| T11 | Extend E2E, doctor, CLI help and docs | T03–T10 | macOS VZ/QEMU matrix, status/error messages, setup, teardown and caveats documented |
| T12 | Functional parity sign-off | All | Apple-equivalent supported workflows proven; deviations/security difference clearly documented |

**Suggested PR sequence:** (1) spikes & architecture decision; (2) runtime + owned VM; (3) internal-network sidecar; (4) host services + security; (5) parity tests + docs.

## 7. Acceptance criteria and non-goals

**Must pass:**

- `--runtime colima` uses a dedicated owned VM, separate from other Colima profiles; profile operations and Docker commands never mutate other profiles or global Docker context.
- Sidecar-mode sandbox has exactly **one** internal-only session network, and Squid is reachable by `egress:3128` without any published egress ports. The sidecar is the only normal path to approved Internet destinations; deny and failure cases fail closed.
- Two simultaneously running sandboxes cannot directly contact one another. A sandbox cannot route through Squid as an arbitrary IP gateway, connect to guest gateway/admin services, or obtain the Docker socket.
- Parent proxy, Ollama and other claimed host-service workflows use independent, strictly limited connectivity. Services that cannot be supported safely are reported as unsupported, not emulated by disabling network restrictions.
- Existing Squid policy semantics, TLS/CA handling, allowlists, reporting, logs, startup/reload/down, and all supported agents/build/workspace workflows are preserved. Network or VM restart does not silently create bypass routes.
- Managed VM mounts include only approved roots and private staging; credentials remain read-only where required; `doctor` and dry-run are non-mutating; VM deletion is explicit and safeguarded.

**Not promised:** Per-sandbox VM/kernel isolation equal to Apple `container`; protection against malicious macOS processes with access to the owned Docker socket; support for containerd/Incus/Kubernetes; changes to ordinary Docker/Podman backends; automatic per-session Colima VMs.

## 8. Examples (proposed CLI, not implemented yet)

```sh
# One-time explicit VM creation
project-sandbox colima init --workspace-root "$HOME/src"

# The Colima backend starts the owned VM as necessary; uses private in-VM egress
project-sandbox --runtime colima --egress sidecar --agent codex \
  "$HOME/src/my-repo" python:3.12-slim

# Observe Docker state without switching the user's context
docker --context colima-project-sandbox ps
docker --context colima-project-sandbox network ls
project-sandbox colima status
project-sandbox colima stop
```

**Network commands below are illustrative validation steps**, not an instruction to manage production resources manually:

```sh
docker --context colima-project-sandbox network create --driver bridge psbx-egress-uplink
docker --context colima-project-sandbox network create --driver bridge --internal psbx-session-demo
# Start a trusted Squid container on psbx-egress-uplink (no --publish), then:
docker --context colima-project-sandbox network connect \
  --alias egress psbx-session-demo project-sandbox-egress
# Start the sandbox on psbx-session-demo only, with HTTPS_PROXY=http://egress:3128
```

## 9. References

- [project-sandbox: `container_cli.py`](https://github.com/pkrusche/project-sandbox/blob/main/src/project_sandbox/container_cli.py)
- [project-sandbox: `egress_sidecar.py`](https://github.com/pkrusche/project-sandbox/blob/main/src/project_sandbox/egress_sidecar.py)
- [project-sandbox: `local_service_network.py`](https://github.com/pkrusche/project-sandbox/blob/main/src/project_sandbox/local_service_network.py)
- [project-sandbox: `squid_config.py`](https://github.com/pkrusche/project-sandbox/blob/main/src/project_sandbox/squid_config.py)
- [Colima profiles](https://colima.run/docs/profiles/); [configuration](https://colima.run/docs/configuration/); [FAQ](https://colima.run/docs/faq/)
- [Docker bridge networking](https://docs.docker.com/engine/network/drivers/bridge/); [Docker `--internal` network behavior](https://docs.docker.com/reference/cli/docker/network/create/)
