# Design

## Context

See proposal.md for motivation and specs/node-npm-flavor/spec.md for requirements.

- `--python-uv` and `--rust-cargo` generate a base Dockerfile (`dockerfile.render_*_dockerfile`) whose final stage is split around sandbox tooling by `Dockerfile.j2`: the sandbox's apt packages, Node.js v26.10.0 (extracted into `/usr/local`), jj, and agent npm packages are installed first, then the generated stage body runs on top. Generated build steps therefore run with the sandbox's Node and npm available.
- Artifacts built into the image live outside `/workspace` (`/opt/venv`, `/opt/cargo-target`) because `/workspace` is a host bind mount at runtime.
- The runtime is firewalled; any lazy download at runtime fails.
- The primary runtime is Apple container on Apple silicon, i.e. linux/arm64 images. Chrome for Testing (Puppeteer's default) has no linux/arm64 build; Debian `chromium` and Playwright's Chromium do.
- Node.js 25+ no longer bundles corepack, which is one reason v1 is npm-only.
- The existing `target/` mask (`cli.py`) shows the pattern for hiding a host directory; Apple container rejects bind mounts onto a missing target.

## Goals / Non-Goals

**Goals:**
- A `--node-npm` flow structurally parallel to `--python-uv`/`--rust-cargo`, so it can share validation, build-context trimming, and generated-file handling.
- Deterministic, lockfile-driven dependency and browser installs.
- A `node_modules` at `/workspace/node_modules` that behaves like a normal local install for Node resolution, bundlers, and npm scripts.

**Non-Goals:**
- Persisting changes to `node_modules` across sessions.
- Supporting layouts that need per-package `node_modules` directories.
- Tuning Chromium launch flags inside user test configs.

## Decisions

### D1. Install into `/opt/node-project`, not `/workspace`
The generated stage sets `WORKDIR /opt/node-project`, copies `package.json`, the lockfile, and workspace member manifests, runs `npm ci`, then resets `WORKDIR /workspace`. Installing under `/workspace` would be hidden by the runtime bind mount; installing at `/node_modules` (relying on Node's upward resolution) breaks npm workspace links and some bundler resolution.

Alternatives: `NODE_PATH` (CJS only, ignored by ESM and bundlers) — rejected.

### D2. tmpfs + entrypoint copy for runtime `node_modules`
The runtime command adds a tmpfs at `/workspace/node_modules` with exec permission and agent-writable mode. The entrypoint, running as the agent user, copies `/opt/node-project/node_modules/.` into it with `cp -a` (preserving relative `.bin` and workspace symlinks), guarded by a check that the target is a mount point and empty.

Why tmpfs over alternatives:
- Named volume with image copy-up: fast after first run, but stale when the lockfile changes unless the volume name tracks a lockfile/image hash, and copy-up semantics on Apple container are unverified.
- Host directory under `.project-sandbox/` populated via `docker cp`: persistent, but writes Linux binaries to the host disk and is slow over VirtioFS for trees with tens of thousands of files.
- Read-only image copy plus per-tool cache env vars: not writable in general; each tool needs its own redirection.

tmpfs needs no lifecycle management and always matches the image. Its cost is memory (D6).

Mount options per runtime:
- Docker/Podman: `--tmpfs /workspace/node_modules:rw,exec,mode=1777`. `exec` is required because Docker's `--tmpfs` defaults to `noexec`, which would break `.bin` scripts and native addons.
- Apple container: `--tmpfs /workspace/node_modules`. Mode and exec defaults are unverified (task 1.1). Fallback if the mount is not agent-writable: a fixed root-owned helper script, allowed via a dedicated sudoers entry like the firewall helper, that chowns the mount point; the helper takes no arguments.

When the host has no `node_modules`, the CLI creates an empty `node_modules` directory in the project only if the runtime requires an existing mount target (verified in task 1.1). It is conventionally gitignored; the CLI prints a note when creating it.

### D3. Manifests-only dependency layer, hard failure
Only manifests and the lockfile are copied before `npm ci`, and no `COPY . .` follows. Unlike `--python-uv`, npm has no "install the project itself" step, so a second whole-project layer adds rebuild cost without benefit. `npm ci` runs lifecycle scripts; a root `prepare`/`postinstall` that needs source files fails the build, which is documented. Supply-chain risk from install scripts is the user's to manage via lockfile pinning.

### D4. npm workspace detection
The CLI reads the root `package.json` `workspaces` field (array or `{ "packages": [...] }`), expands globs relative to the project root with `Path.glob`, keeps directories that contain a `package.json`, and emits one `COPY <member>/package.json <member>/` per member — mirroring `_detect_cargo_workspace`. Negated patterns are honoured by removing matches. Relative workspace symlinks created by npm under `/opt/node-project/node_modules` resolve to `/workspace/<member>` after the copy.

### D5. Browser layers
- Always: `apt-get install chromium fontconfig fonts-liberation fonts-noto-color-emoji`.
- If the lockfile's `packages` map contains `node_modules/playwright` or `node_modules/@playwright/test` (checked by parsing the lockfile JSON in the CLI): after `npm ci`, run `npx --no-install playwright install --with-deps chromium` from `/opt/node-project`, so the browser revision comes from the project's locked Playwright.
- `ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, `CHROME_BIN=/usr/bin/chromium`, `PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium`, `PUPPETEER_SKIP_DOWNLOAD=true` (Chrome for Testing has no arm64 build, and the download would target `/root`).
- `/opt/ms-playwright` and `/opt/node-project` are chowned to `AGENT_UID:AGENT_GID` at the end of the stage.

Because the generated stage body runs after the sandbox Node install (see Context), `npm` and `npx` are available.

### D6. `--shm-size`
Accepted for any image-based runtime; default `2g` only under `--node-npm` so existing sessions are unchanged. Passed as `--shm-size <value>` for Docker, Podman, and Apple container. Validation accepts the `<number>[bkmg]` form (case-insensitive) and rejects chroot.

### D7. Build context and ignore file
`--node-npm` joins the whole-project-context flows that get the generated `Dockerfile.dockerignore` (`node_modules` is already listed), so host binaries never enter the build. `--branch` uses the worktree as the asset project, as `--python-uv` does.

## Risks / Trade-offs

- [tmpfs memory: `node_modules` plus `/dev/shm` count against `--memory` (default 8g)] → Document it; print the baked `node_modules` size after a build; users can raise `--memory`.
- [Session start cost: copying a large `node_modules` takes seconds] → Acceptable for v1; a volume-based cache keyed by lockfile hash is a possible follow-up.
- [Apple container tmpfs may be root-owned or `noexec`] → Spike first (task 1.1); sudoers-guarded fixed helper as fallback (D2).
- [Chromium needs `--no-sandbox` as a non-root user without user namespaces] → Document in `docs/security.md`, including that the container boundary is the isolation layer.
- [Playwright `install --with-deps` may not support the base image's Debian release] → Base on the same Debian release Playwright supports; the build fails loudly if not.
- [Image size grows by several hundred MB] → Documented; only Playwright's Chromium is conditional.
- [Root lifecycle scripts needing source fail the build] → Documented supported-repo shapes; users can move such scripts out of `prepare`.

## Migration Plan

Additive flag; no migration. Rollback is removing the flag.

## Open Questions

- Exact Debian release for the generated base (`debian:bookworm-slim` vs `trixie-slim`) — pick whichever current Playwright `install-deps` supports on arm64 at implementation time.
