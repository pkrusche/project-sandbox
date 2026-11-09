# Design

## Context

Current flow (`Dockerfile.j2:128–147`, `entrypoint.sh.j2:30–41`, `container_cli.py:395–420`):

```
runtime run --workdir /workspace --cap-add NET_ADMIN --cap-add NET_RAW   (image USER agent)
  └ project-sandbox-entrypoint            uid=agent, cwd=/workspace
      ├ provisioning (_provision.sh.j2), node_modules copy
      ├ sudo -n project-sandbox-init-firewall      ── uid 0, cwd still /workspace
      │    ├ python3 - <<PY_ENDPOINTS              ── sys.path[0] = '' → /workspace
      │    └ python3 - <<PY_PREFLIGHT              ── same
      └ exec agent / shell                         ── sudo rule still usable
```

`sudo` resets the environment by default on Debian-family images, but it preserves the working directory. That is the root cause of finding #1. The sudoers rule stays in force for the whole session, so the agent can trigger the root path again at any time.

The devcontainer used the same rule from `postStartCommand`. `minimal-devcontainer` lands first and removes its firewall, so the rule has no remaining user.

## Goals / Non-Goals

**Goals:**
- No root code path in an agent session is reachable after the session command starts.
- Root code never reads code or configuration from the workspace, including files left there by an earlier session.
- Old images cannot be started under the new launch argv.

**Non-Goals:**
- Changing *what* the firewall allows. That belongs to `scope-firewall-allowlist`.
- Hardening the chroot backend, which is not a security boundary.
- Dropping `NET_ADMIN`/`NET_RAW` from the container itself. The root stage needs them, and the drop removes them from the agent's bounding set.

## Decisions

### 1. Root stage at container start, then an irrevocable drop

New flow:

```
runtime run --user 0:0 --workdir /workspace --cap-add NET_ADMIN --cap-add NET_RAW
  └ project-sandbox-init (root stage)        uid=0
      ├ cd /
      ├ env -i PATH=<fixed> HOME=/root \
      │     /usr/local/bin/project-sandbox-init-firewall      (unless --no-firewall)
      └ exec setpriv --reuid=agent --regid=<gid> --init-groups \
              --inh-caps=-all --ambient-caps=-all --bounding-set=-all \
              --no-new-privs -- /usr/local/bin/project-sandbox-entrypoint "$@"
                 └ provisioning, node_modules, agent           uid=agent, no sudo
```

- The image keeps `USER agent`. `container_cli.run_argv` adds `--user 0:0` only for agent sessions. A host-side `exec` still lands as `agent`, and an image run without project-sandbox's argv never starts as root by accident.
- The root stage returns to `/workspace` only after the drop. The entrypoint and agent need it as their cwd, so `setpriv` is followed by `cd /workspace` in the entrypoint, or the entrypoint sets the directory itself.
- `setpriv` (util-linux) is preferred over `su`/`runuser`, which go through PAM and keep a parent process alive, and over a Python drop helper, which would add another privileged interpreter invocation to harden. `setpriv` execs directly, so PID 1 becomes the entrypoint, as today. That matters for signal handling and the existing timeout teardown.
- `--no-new-privs` makes setuid binaries, including `sudo`, inert even if a rule survived. With an empty bounding set, file capabilities cannot regrant `NET_ADMIN`.

**Alternatives considered:**
- *Only add `-I` and `cd /` to the script, and keep `sudo`.* This fixes #1 but leaves a root entry point that the agent can trigger whenever it likes. Every future change to the firewall script would re-open the question. Rejected as the final state. It is still applied as defense in depth (Decision 2).
- *`--security-opt no-new-privileges` on the runtime.* This would break the root stage's own needs on some runtimes, and Apple `container` has no equivalent flag. In-container `setpriv` is portable across all three runtimes.

### 2. The firewall script is self-protecting, too

The firewall script starts with `cd /` and runs both helpers as `python3 -I -`. The root stage already sanitizes, but the script must stay safe if a future caller forgets to.

`-I` implies `-E -s`. It drops the script directory/cwd from `sys.path`, ignores `PYTHON*` variables and skips user site-packages.

Audit of the other root-run commands in the script: `iptables`, `ip6tables`, `ipset`, `ip`, `getent`, `awk`, `sysctl`, `curl`, `timeout`, `install` and `update-ca-certificates`. All use absolute input paths. They are resolved through the fixed `PATH`, and none reads the cwd.

`curl` is the exception: it reads `$HOME/.curlrc`, which can name an output file, a further `--config` or a proxy. All three root-run calls (`init-firewall.sh.j2:154`, `:331` and `:382`) therefore start with `curl -q`, which disables `.curlrc` and must be the first argument. On top of that, the root stage sets `HOME=/root`.

Tests pin the fixed `PATH`, assert that no relative path appears in root-run commands, and assert that every `curl` in the rendered script is `curl -q`.

### 3. Image marker for the privilege model

`Dockerfile.j2` adds `LABEL org.project-sandbox.privilege-model="root-stage-v1"`.
- Before adding `--user 0:0`, the launch checks the label with the existing `image inspect` call (`container_cli.image_exists`).
- If the label is missing, the launch is refused with a rebuild hint.
- The label is part of the rendered Dockerfile, so it also changes the build-cache fingerprint. Cache-hit paths therefore rebuild without a separate check.

This is the only safe option for `--no-build`. Starting an old image with `--user 0:0` would run its old entrypoint, and therefore the agent, as root.

## Risks / Trade-offs

- [`setpriv` missing in a custom base image, such as Alpine or a distroless-derived image] → Install `util-linux` in the existing apt step. The build fails if `setpriv` is still absent after the provisioning layer (`command -v setpriv`). Non-Debian bases are already unsupported by the apt-based Dockerfile.
- [The root stage may not start as root, or may be unable to drop its bounding set, on some runtime] → Dropping the bounding set needs `CAP_SETPCAP`. Task 0.1 checks `--user 0:0` and the full `setpriv` drop on Apple `container`, Docker and rootless Podman (with `--userns=keep-id`) before anything else is built. If a runtime can't do it, the design is revisited for that runtime before implementation.
  - **Spike result, 2026-11-09:** all three runtimes passed. A throwaway `debian:trixie-slim` image with `util-linux` and `iptables`, `USER agent` (uid 501, gid 20), was run with `--user 0:0 --cap-add NET_ADMIN --cap-add NET_RAW` on Docker Desktop 29.9.3, Podman 6.0.0 machine (rootless, plus `--userns=keep-id`) and Apple `container` 1.7.1. Each time, `iptables -L` worked as root. After `setpriv --reuid 501 --regid 20 --init-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs`, `/proc/self/status` showed `Uid`/`Gid` 501/20 in all four fields, `Groups: 20`, all five capability sets zero, `NoNewPrivs: 1`, and `iptables -L` was refused.
  - Root-stage bounding sets differ: Docker and Apple show `CapBnd 0xa80435fb`, Podman `0x800435fb` (no `MKNOD` or `AUDIT_WRITE`). Under `keep-id`, Podman's root stage also carries the supplementary group 1000 (`core`); `--init-groups` drops it. Not covered: native Linux hosts and rootful Podman.
- [`env -i` drops a variable the firewall needs] → Today the rendered script reads no environment variables: every setting is rendered into it at build time. A renderer test asserts that the script never references `$PROJECT_SANDBOX_*` or other caller environment, so a future change cannot start depending on one without noticing.
- [PID 1 semantics change] → `setpriv` uses `exec`, so the entrypoint is still PID 1 after the drop, and the timeout teardown (`scripts/verify-timeout-teardown.sh`) still applies.
- [Users who relied on running `sudo …init-firewall` manually inside a session] → They can no longer do that. Re-initializing mid-session is exactly what this change removes. Document it.

## Migration Plan

- No user action is needed for normal launches: the fingerprint changes and images rebuild.
- `--no-build` users get a clear refusal once, then rebuild.
- To roll back, revert the change. Old images are not affected, because the label is only checked by the new CLI.
