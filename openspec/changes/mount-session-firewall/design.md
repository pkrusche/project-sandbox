# Design

## Context

- `firewall.render` writes `init-firewall.sh` into the generated build context,
  and `Dockerfile.j2` copies it to `/usr/local/bin/project-sandbox-init-firewall`
  with a sudoers rule for exactly that path.
- The entrypoint runs it with `sudo -n`; the script installs the policy, removes
  its sudoers entry, and an atomic guard in `/run` refuses a second run.
- `--no-firewall` makes the host pass `PROJECT_SANDBOX_NO_FIREWALL=1`, and the
  entrypoint skips the call.
- `session_inputs.allocate()` gives each launch a private 0700 directory
  outside the project, already mounted for configs and prompts.

## Goals / Non-Goals

**Goals:** no network policy inside any image; a session can't start its
command without the firewall it asked for.

**Non-Goals:** changing what the firewall admits; the sidecar's own policy;
replacing sudo with a root stage (`isolate-firewall-helpers` on the branch).

## Decisions

### 1. Mounted script, baked launcher

- The host renders `init-firewall.sh` into `<session inputs>/firewall/` and
  mounts that directory read-only at `/project-sandbox-firewall` (a directory
  mount, so Apple `container` accepts it).
- The image keeps the sudoers rule for
  `/usr/local/bin/project-sandbox-init-firewall`, now a short root-owned
  launcher. It checks that `/project-sandbox-firewall/init-firewall.sh` is a
  regular file, then executes it with `cd /`, a fixed `PATH` and the existing
  environment reset. The script keeps the run-once guard and the sudo
  revocation, so the launcher can't be run twice either.
- The image keeps the tools the script needs (`iptables`, `ipset`, `dig`,
  `python3`).

*Alternative:* point the sudoers rule at the mounted path. Rejected: the rule
would then name a path whose content comes from a mount, and a missing mount
would leave a sudo-able path an agent could create if a parent were writable.

### 2. Fail closed

The entrypoint, which runs before any agent process, reads
`PROJECT_SANDBOX_NO_FIREWALL` as the host set it. If unset, it runs the
launcher and exits non-zero when the launcher fails or the script is missing.

### 3. Build cache and labels

`build_cache._BUILD_INPUT_FILES` drops `init-firewall.sh`; the launcher is part
of the Dockerfile and doesn't vary. New images carry
`LABEL org.project-sandbox.firewall-delivery=mounted`. Under `--no-build`,
`container_cli` inspects the pinned image's labels and refuses one without it.
`--dry-run` prints the label it would require without inspecting.

## Risks / Trade-offs

- [A runtime mounts the inputs directory late] → The launcher's missing-script
  check stops the session with the path.
- [Someone runs the image by hand without the mount] → It exits with a message
  naming `--no-firewall`.
- [Rootless Podman can't read the mounted script as root] → Session inputs are
  already readable there for configs; the e2e check covers all three runtimes.

## Migration Plan

Images rebuild once. `--no-build` against an older image is refused with a
rebuild instruction (Decision 3).
