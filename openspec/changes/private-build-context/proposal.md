# Proposal

## Why

Whole-project builds (`--python-uv`, `--rust-cargo`, `--node-npm`, `--dockerfile`) use the project, or a `--docker-context` directory, as their build context. The generated Dockerfile copies project-sandbox's generated files by a path relative to that context, so they must exist in `<context>/.project-sandbox/`.

After `private-session-state`, those files are rendered privately and copied into the context right before the build. Two gaps of security finding #6 (inputs half) remain for these flows:
- a concurrent launch, or anything else that can write the context, can replace a file between the copy and the build;
- the host still writes into the project, and `--docker-context` must still contain `.project-sandbox`.

This change was split from `private-session-state` on 2026-11-09, because it depends on a runtime capability that Apple `container` lacks and that needs a spike.

## What Changes

- **Generated files reach the build as a separate input.**
  - On Docker and Podman, the session's private `build/` directory is passed as a second, named build context, and the generated Dockerfile copies from it.
  - Apple `container` 1.7.1 has no `--build-context`. There the generated files come from a content-addressed assets image (`COPY --from=<assets>`). The assets reference is a `localhost/` tag, so a missing tag fails instead of being pulled from Docker Hub (spike, 2026-11-09).
- **Nothing is written into a build context.** The interim copy into `<context>/.project-sandbox/` is removed. A leftover directory from an older version is never read.
- **`--docker-context` no longer has to contain `.project-sandbox`.**

## Capabilities

### New Capabilities

- `build-input-delivery`: how image builds receive generated files when the build context is the project or a user-supplied directory.

### Modified Capabilities

(none. `private-session-state`'s "Generated state lives outside the project" requirement names this change as the end of its `.project-sandbox/` exception. That sentence is removed from the main spec when this change is archived; see the workflow follow-up in tasks.md.)

## Impact

- **Code:**
  - `container_cli.py`: `--build-context` argv for Docker and Podman, `--ignorefile` on Podman, and the Apple mechanism.
  - `dockerfile.py` and `templates/Dockerfile.j2`: `sandbox_copy_prefix` becomes `sandbox_copy_from`.
  - `cli.py`: the interim copy and the `--docker-context` check are removed.
- **Depends on:** `private-session-state` (the session `build/` directory).
- **Enables:** `pin-launch-image-identity`, whose fingerprint then covers exactly what a build reads.
- **Docs:** `docs/usage.md` (`--docker-context`), `docs/security.md`, `docs/development.md` (spike results).
