# Proposal

## Why

Agents working on npm-based web projects cannot install dependencies or download headless browsers inside the sandbox, because runtime networking is firewalled. They also cannot reuse the host's `node_modules`, whose native binaries (esbuild, rollup, Playwright) are built for the host platform. `--python-uv` and `--rust-cargo` already solve the equivalent problem for Python and Rust by baking dependencies into a generated image; npm projects with headless browser tests need the same.

## What Changes

- New `--node-npm` flag that synthesises a base Dockerfile for npm projects, mutually exclusive with `base_image`, `--dockerfile`, `--python-uv`, and `--rust-cargo`, and rejected with `--runtime chroot`.
- Uses the sandbox's pinned Node.js (v26) — no per-project Node version selection, no corepack, npm only.
- Requires `package.json` plus `package-lock.json` or `npm-shrinkwrap.json`; fails instead of warning when either is missing.
- Installs dependencies at image build time with `npm ci` from the manifests only, into `/opt/node-project/node_modules`; a failing `npm ci` fails the build.
- At runtime, mounts a tmpfs over `/workspace/node_modules` and has the entrypoint copy the baked `node_modules` into it, so the directory is Linux-native, writable (tool caches such as `.vite`, `.cache`), and hides the host's copy. Writes do not persist across sessions.
- Bakes headless browser support into the image: Debian `chromium`, fontconfig, `fonts-liberation`, `fonts-noto-color-emoji`; plus Playwright's Chromium via the project's own Playwright CLI when the lockfile contains `playwright` or `@playwright/test`. Browser caches live under `/opt` and are owned by the agent user.
- Sets `CHROME_BIN`, `PLAYWRIGHT_BROWSERS_PATH`, and `PUPPETEER_EXECUTABLE_PATH` (pointing at system Chromium); does not set `CI`.
- New `--shm-size` flag (default `2g`) passed to Docker, Podman, and Apple container for `--node-npm` sessions.
- Documentation of supported repo shapes (single root `node_modules`, including npm workspaces), firewall behaviour with example `--internet-proxy` configurations, Chromium's `--no-sandbox` requirement, and the tmpfs memory trade-off.

Out of scope for v1: pnpm/yarn/bun, per-package `node_modules` layouts, Node version selection, devcontainer generation, Puppeteer/Cypress browser downloads, CJK fonts.

## Capabilities

### New Capabilities
- `node-npm-flavor`: The `--node-npm` flag — CLI validation, generated Dockerfile contents (dependency install and headless browser layers), runtime `node_modules` tmpfs mount and copy, and `--shm-size` handling.

### Modified Capabilities
<!-- None: --dockerfile splicing, credential forwarding, and proxy behaviour are unchanged. -->

## Impact

- `src/project_sandbox/cli.py`: new flags, validation in `_resolve_build_source`, build-context trimming, runtime mounts, chroot rejection.
- `src/project_sandbox/dockerfile.py`: new `render_node_npm_dockerfile`.
- `src/project_sandbox/container_cli.py`: `--tmpfs` and `--shm-size` argv for Docker, Podman, and Apple container.
- `src/project_sandbox/templates/entrypoint.sh.j2`: populate `/workspace/node_modules` from `/opt/node-project/node_modules`.
- `docs/usage.md`, `docs/security.md`, `docs/runtime.md`, `docs/internet-proxy.md`; tests in `tests/`.
- Image size grows by Chromium, fonts, and optionally Playwright's Chromium (several hundred MB); container memory usage grows by the size of `node_modules` held in tmpfs.
