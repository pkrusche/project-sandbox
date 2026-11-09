# Proposal

## Why

The rendered firewall script is `COPY`ed into the session image. Network policy is therefore part of image identity:

- **Any policy change means another image.** Changing extra domains, a proxy, Ollama or the agent rebuilds the image and moves the project's shared tag.
- **Stale-policy images.** `--build-only --pi-ollama` builds an image without the Ollama firewall rule, because the Pi + Ollama setup is enabled only with `--agent pi`, which `--build-only` rejects. A later `--agent pi --pi-ollama --no-build` session reuses that image, and Ollama is refused with "No route to host" (TODO.md, first item). The same mismatch can happen for every setting rendered into the firewall.
- **A wrong image is a wrong firewall.** If a concurrent launch moves the tag (finding #6, launch half), a session gets another session's network policy.

This change was split from `scope-firewall-allowlist` on 2026-11-09. Landing it before `pin-launch-image-identity` means that change never has to record network settings in image labels.

## What Changes

- **The firewall script is a per-session runtime input.**
  - It is rendered into the session's private staging directory, and mounted read-only at `/project-sandbox-firewall/`.
  - The root stage (`isolate-firewall-helpers`) runs it from there.
  - Images no longer contain a firewall script or any network allowlist. Changing domains, proxies, Ollama or egress mode never rebuilds the image.
- **Fail closed.** The root stage runs the mounted script unless the host started the container with the firewall explicitly disabled (`--no-firewall`). If the script is missing, the container exits before the session command.
- **The stale-image case can't occur.** `--build-only --pi-ollama` followed by `--agent pi --pi-ollama --no-build` works, because the Ollama rule comes from the session, not the image.
- **What the firewall admits is unchanged.** That is `scope-firewall-allowlist`.

## Capabilities

### New Capabilities

- `session-firewall-delivery`: how a session's firewall policy reaches the sandbox (a per-session, read-only runtime input run by privileged setup), and what happens when it is missing.

### Modified Capabilities

(none)

## Impact

- **Code:**
  - `firewall.py` and `cli.py`: render into `staging/<id>/firewall/`; the mount.
  - `container_cli.py`: the read-only mount whenever the firewall is enabled.
  - `templates/Dockerfile.j2`: the firewall `COPY` is removed. The tools the script needs stay in the image.
  - The root-stage template: the script path, and the missing-script check.
  - `build_cache.py`: the firewall script leaves the fingerprinted build inputs.
- **Depends on:**
  - `isolate-firewall-helpers` (the root stage, with its `-I`, `cd /` and `env -i` protections);
  - `private-session-state` (the private staging the script is mounted from).
- **Interplay:**
  - `validate-all-mount-sources` forbids user mount targets under `/project-sandbox-*`.
  - `pin-launch-image-identity`: its `settings` label carries no network fields.
- **Images:** rebuild once, because the Dockerfile changes.
- **TODO.md:** the first item (`--build-only --pi-ollama`) is resolved.
- **Docs:** `docs/security.md` (runtime firewall delivery), `docs/usage.md` ("Image Builds").
