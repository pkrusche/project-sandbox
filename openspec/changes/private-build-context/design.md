# Design

## Context

- `container_cli.build_image` runs `build` with `cwd` set to the context and `.` as the context argument. Apple `container` "only reliably honors a current-directory context" (`container_cli.py:510`).
- Whole-project builds `COPY {{ sandbox_copy_prefix }}<file>`, a path relative to the context.
- After `private-session-state`, the generated files live in the session's `staging/<id>/build/`, and are copied into `<context>/.project-sandbox/` right before a whole-project build.

Spike, 2026-11-09:
- Docker 29.9.2 (buildx 0.38.1) and Podman 6.0.0 both support `--build-context name=path`.
- Apple `container` 1.7.1 `build` has no `--build-context`. Its options are `--build-arg`, `-f`, `--target`, `--secret`, `--ssh`, `--label` and output/platform flags.
- Whether Apple's builder resolves a locally built image in `COPY --from=<image>` has not been tested yet. That is task 1.1.

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
| Apple `container` | **Primary:** build a content-addressed assets image `project-sandbox-assets-<project-hash>:<fingerprint12>` (`FROM scratch` plus `COPY`) from `build/`, then `COPY --from=<assets ref>`. **Fallback:** a private, cloned build context (below). |

- `sandbox_copy_prefix` in `Dockerfile.j2` becomes `sandbox_copy_from`, either `--from=sandbox `/`--from=<ref> ` or empty for the default flow.
- The assets image tag is content-addressed, so concurrent sessions that produce the same tag produce the same content.
- Old assets tags are pruned after a successful build: keep the last N per project.

### 2. Apple fallback: a cloned context

If task 1.1 shows that Apple's builder can't resolve local images:
- copy the project into `build/context/` with APFS `clonefile` (`cp -c -R`, copy-on-write, near-instant on the same volume), honoring the generated ignore rules;
- add the generated files;
- build from there, cwd-based, which is what Apple handles reliably;
- if the project is on another volume, the clone degrades to a copy, and the CLI warns with the size.

### 3. Ignore files

Dockerfile-specific ignore files (`<Dockerfile>.dockerignore` next to the Dockerfile) sit in the private `build/`.
- BuildKit (Docker) reads them next to `-f`.
- Podman needs `--ignorefile`.
- For Apple, task 1.1 checks whether an absolute `-f` outside the context and its ignore file are honored. If they aren't, the clone fallback writes `.dockerignore` into the cloned context instead.

## Risks / Trade-offs

- [Apple's builder can't use local images, and the clone fallback is slow for huge projects on another volume] → Warn with the size. Document keeping projects and `$HOME` on one APFS volume. The spike decides before any code is written.
- [Assets images accumulate] → Pruned per project after each successful build.
- [A user Dockerfile stage named `sandbox` collides with the named context] → The splicer already owns stage names it inserts; a test covers a user stage of that name, and the context name is changed if it collides.

## Migration Plan

Images rebuild once, because the generated Dockerfile changes. A `.project-sandbox/` directory left in a project by the interim copy is removed by `private-session-state`'s known-file cleanup on the next launch.
