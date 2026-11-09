# Design

## Context

- `firewall.render` (`cli.py:763`) renders `init-firewall.sh` from `init-firewall.sh.j2` into the build context, and `Dockerfile.j2` `COPY`s it into the image (`:128`).
- After `isolate-firewall-helpers`, a root stage runs `/usr/local/bin/project-sandbox-init-firewall` under `env -i`, then drops privileges for good.
- The host passes `--env PROJECT_SANDBOX_NO_FIREWALL=1` when `--no-firewall` is given (`container_cli.py:463`).
- After `private-session-state`, each session has a private `staging/<id>/` directory.

## Goals / Non-Goals

**Goals:**
- No network policy inside any image.
- A session can't start its command without the firewall it asked for.

**Non-Goals:**
- Changing what the firewall admits (`scope-firewall-allowlist`).
- The sidecar's own policy, which already lives outside images.

## Decisions

### 1. Mount the rendered firewall instead of baking it

- **Where it's written:** `staging/<id>/firewall/init-firewall.sh`, mounted read-only at `/project-sandbox-firewall`. A directory mount, so it works on Apple `container`.
- **Who runs it:** the root stage runs `/project-sandbox-firewall/init-firewall.sh` with `env -i`, `cd /` and `python3 -I`, exactly as it ran the baked path.
- **Image:** `Dockerfile.j2` drops the firewall `COPY`. The image keeps the tools the script needs (`iptables`, `ipset`, `dig`, `python3`).

**Why this is safe:**
- The script comes from the host's private, per-session staging.
- It's mounted read-only, and runs before any agent process exists.
- User mounts can't target `/project-sandbox-*` (`validate-all-mount-sources`).

**Alternative considered:** keep baking the script. Every allowlist change then rebuilds and moves the shared tag, and network policy stays part of image identity, the source of the stale-image item. Rejected.

### 2. Fail closed

The root stage decides from what the host passed at container start, never from the image:
- `PROJECT_SANDBOX_NO_FIREWALL=1` set by the host: skip the firewall, then drop privileges.
- otherwise: the script must exist and be a regular file. If not, print the reason and exit non-zero before the drop.

The variable is read once by the root stage, before `env -i`. An agent can't influence it: the root stage finishes before any agent process starts.

### 3. Build cache

`build_cache._BUILD_INPUT_FILES` loses `init-firewall.sh`. Two sessions that differ only in network settings now share one image.

## Risks / Trade-offs

- [A runtime mounts the staging directory late or not at all] → The missing-script check stops the session. The mount probe from `private-session-state` reports unmountable staging earlier, with the path.
- [Someone runs the image by hand without the mount] → The container exits with the missing-script message, which names `--no-firewall`.
- [Rootless Podman can't read the mounted script as container root] → Covered by the e2e check on all three runtimes (task 2.2).

## Migration Plan

Images rebuild once. `--no-build` against an image that still contains a baked firewall is refused through the existing privilege-model label check: the label value changes to `root-stage-v2`.
