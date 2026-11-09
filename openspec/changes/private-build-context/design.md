# Design

## Context

- `container_cli.build_image` runs `build` with `cwd` set to the context and `.` as the context argument. Apple `container` "only reliably honors a current-directory context" (`container_cli.py:510`).
- Whole-project builds `COPY {{ sandbox_copy_prefix }}<file>`, a path relative to the context.
- After `private-session-state`, the generated files live in the session's `staging/<id>/build/`, and are copied into `<context>/.project-sandbox/` right before a whole-project build.

Spike, 2026-11-09:
- Docker 29.9.2 (buildx 0.38.1) and Podman 6.0.0 both support `--build-context name=path`.
- Apple `container` 1.7.1 `build` has no `--build-context`. Its options are `--build-arg`, `-f`, `--target`, `--secret`, `--ssh`, `--label` and output/platform flags.
- Task 1.1, 2026-11-09 (Apple `container` 1.7.1):
  - A `FROM scratch` assets image built from a temporary directory works. A second build with `COPY --from=<assets tag>` takes the file from the local image store (`oci-layout://…`), not from a registry. After the tag was rebuilt with new content, the next build used the new content.
  - If the tag is missing locally, the builder falls back to the registry: an unqualified name goes to `registry-1.docker.io/v2/library/<name>` (401 here). With a `localhost/` name, a missing tag fails with "Connection refused" instead.
  - `name:tag@sha256:<digest>` is not resolved from the local store. The builder goes to the registry even when the digest matches the local image, so digest pinning can't be used.
  - An absolute `-f` outside the cwd context works. Its `<Dockerfile>.dockerignore` is honored and takes precedence over the context's `.dockerignore`, as on Docker 29.9.
  - The spike images were deleted afterwards.

## Goals / Non-Goals

**Goals:**
- A build reads generated files only from the session's private directory.
- The host writes nothing into any build context.

**Non-Goals:**
- Image identity and the launch tag (`pin-launch-image-identity`).
- The default flow, whose context already is the private `build/` directory.

## Decisions

### 1. Supplying generated files

| Runtime | Whole-project or `--docker-context` build |
|---|---|
| Docker, Podman | `--build-context sandbox=<build/>`; the template emits `COPY --from=sandbox <file> …` |
| Apple `container` | Build a content-addressed assets image `localhost/project-sandbox-assets-<project-hash>:<fingerprint12>` (`FROM scratch` plus `COPY`) from `build/`, then `COPY --from=<assets ref>`. |

- `sandbox_copy_prefix` in `Dockerfile.j2` becomes `sandbox_copy_from`, either `--from=sandbox `/`--from=<ref> ` or empty for the default flow.
- The assets image tag is content-addressed, so concurrent sessions that produce the same tag produce the same content.
- The assets reference always carries the `localhost/` registry part. Then a tag missing from the local store fails the build instead of being pulled from Docker Hub. Digest references aren't resolved locally (Context), so the reference is a tag.
- Old assets tags are pruned after a successful build: keep the last N per project.

### 2. Not chosen: a cloned context on Apple

Copying the project into a private context with APFS `clonefile` (`cp -c -R`) was the fallback in case Apple's builder couldn't resolve local images. Task 1.1 showed it can, so the clone isn't built. The clone would also degrade to a full copy for projects on another volume.

### 3. Ignore files

Dockerfile-specific ignore files (`<Dockerfile>.dockerignore` next to the Dockerfile) sit in the private `build/`.
- BuildKit (Docker) reads them next to `-f`.
- Podman needs `--ignorefile`.
- Apple honors an absolute `-f` outside the context and the ignore file beside it (task 1.1), so it gets the same layout as Docker.

## Risks / Trade-offs

- [The assets tag is missing when the second build runs, for example after a concurrent prune] → The `localhost/` name makes the build fail instead of pulling. The CLI rebuilds the assets image once and retries. Pruning keeps the tag the current build uses.
- [A process listening on the host's `localhost` registry port serves an image under that name] → It would need to run as the same user, who could change the build anyway. Accepted.
- [Assets images accumulate] → Pruned per project after each successful build.
- [A user Dockerfile stage named `sandbox` collides with the named context] → The splicer already owns stage names it inserts; a test covers a user stage of that name, and the context name is changed if it collides.

## Migration Plan

Images rebuild once, because the generated Dockerfile changes. A `.project-sandbox/` directory left in a project by the interim copy is removed by `private-session-state`'s known-file cleanup on the next launch.
