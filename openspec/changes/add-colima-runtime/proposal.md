# Proposal

## Why

macOS users who run Docker through Colima can already point project-sandbox at the `docker` CLI, but the egress sidecar's network path is built for hosts: Squid is published on a host or gateway address and sandboxes reach it as `host.docker.internal:<port>`. In corporate environments, application firewalls and endpoint-security software interfere with that hop, and the setup is fragile. A Colima VM runs the sandboxes and the sidecar on one Linux Docker engine, so sandbox-to-sidecar traffic can stay on a private Docker bridge inside the VM and never cross a host-network boundary.

## What Changes

- Add `--runtime colima`. It drives the `docker` CLI against a **user-created** Colima profile (default `project-sandbox`, override with `--colima-profile NAME`) through an explicit `docker --context colima-<profile>` on every invocation, never switching the user's global Docker context.
- Verify the profile before any container work: the context exists, the engine answers, and the engine is the profile's Colima VM. Fail with the `colima start` command to run when the profile is stopped or missing. project-sandbox does not create, start, stop, reset or reconfigure the VM.
- Add a **private-network sidecar strategy** for `--egress sidecar` on Colima:
  - Two persistent, labelled networks in the VM: `psbx-egress-uplink` (ordinary bridge, used only by Squid for outbound traffic) and `psbx-sandbox` (`--internal` bridge, no route out).
  - The sidecar is created with both networks and the alias `egress` on `psbx-sandbox`, and **publishes no port**.
  - Sandboxes join `psbx-sandbox` only and reach Squid at `egress:3128`. The existing in-container firewall stays on as defense in depth and admits only the sidecar's address and port.
- Colima sidecar state, images and locks are scoped by profile.
- An explicit non-default `[egress.sidecar] port` is an error on Colima, because the proxy listens on a fixed unpublished port.
- Features that need a path from the sandbox to macOS are **unsupported on Colima** and fail before any container work with a message naming the feature: Ollama, `--agent-proxy` to a macOS-local gateway, and an upstream parent proxy bound to macOS loopback. An upstream proxy reachable by network address from the VM works.
- `--egress firewall` on Colima behaves as on plain Docker; sidecar-only isolation does not apply to it.
- Preflight check that the paths a launch mounts (workspace, staging, `.git` for `--branch`) are visible in the VM, with an error naming the Colima mount to add.
- Document setup, the supported/unsupported matrix and the security properties in `docs/`.
- Run technical spikes before the implementation tasks, and record verdicts in `docs/`.

Out of scope: per-session networks and sandbox-to-sandbox isolation, `colima init`/`reset`/managed VM lifecycle, mount management, a host-service adapter, per-sandbox VM isolation equal to Apple `container`.

## Capabilities

### New Capabilities

- `colima-runtime`: selecting and verifying a Colima profile as a Docker runtime: explicit context targeting, profile verification, mount preflight, and the list of features unsupported on Colima.

### Modified Capabilities

- `egress-sidecar`: adds a private in-VM network endpoint (`egress:3128`, no published port) for the Colima runtime as an alternative to the published endpoint, including how the sandbox reaches and is restricted to it, the fixed-port rule, and profile-scoped sidecar state.

## Impact

- Code: `container_cli.py` (runtime identity and context-aware argv), `egress_sidecar.py` (new strategy, network setup, profile-scoped layout, readiness probe without a published port), `cli.py` (sandbox plan and network args near the sidecar plan, unsupported-feature checks), `local_service_network.py` and `doctor.py` (runtime-specific probes), plus the sidecar's `[egress.sidecar] port` validation.
- Specs: new `colima-runtime`; delta to `egress-sidecar`. Other runtimes' behavior is unchanged.
- Docs: new Colima page under `docs/`, README pointer, CHANGELOG entry.
- Tests: argv construction and dry-run tests with a mocked runner; opt-in end-to-end suite requiring a Colima profile.
- Dependencies: none new. Requires Colima with the Docker runtime on the user's machine.
