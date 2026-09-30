# Tasks

## 1. Apple container spike

- [ ] 1.1 On macOS with Apple container, run a Debian image as UID 1000 with `--tmpfs /workspace/node_modules` over a bind-mounted `/workspace` and `--shm-size 2g`; record in design.md whether the tmpfs is writable by UID 1000, whether it allows exec, whether a missing mount target is rejected, and the observed `/dev/shm` size (`df -h /dev/shm`). Verify by updating design.md D2 with the findings and the chosen permission approach (mount option vs sudoers helper).

## 2. CLI flags and validation

- [x] 2.1 Add `--node-npm` and `--shm-size` to `build_parser`, and extend `_resolve_build_source` with mutual-exclusion checks against `base_image`, `--dockerfile`, `--python-uv`, and `--rust-cargo`; verify with `tests/test_cli.py` cases for each conflict.
- [x] 2.2 Reject `--node-npm` and `--shm-size` with `--runtime chroot`, and validate the `--shm-size` format; verify with tests for chroot rejection and valid/invalid sizes.
- [x] 2.3 Fail when `package.json` or both lockfiles are missing, accepting `npm-shrinkwrap.json` as an alternative; verify with tests asserting the error message and that no Dockerfile is written.
- [x] 2.4 Implement npm workspace detection (array and `{packages}` forms, globs, negations, members without `package.json` skipped); verify with unit tests over temporary project trees.
- [x] 2.5 Detect `playwright` / `@playwright/test` in the lockfile `packages` map; verify with tests for present, absent, and nested-dependency-only cases.

## 3. Generated Dockerfile

- [x] 3.1 Add `render_node_npm_dockerfile` producing the Debian base, Chromium and font packages, browser env vars, manifests-only `npm ci` layer in `/opt/node-project`, conditional Playwright install, agent ownership of `/opt/node-project` and `/opt/ms-playwright`, and `WORKDIR /workspace`; verify with `tests/test_renderers.py` asserting layer contents and ordering for plain, workspace, and Playwright projects.
- [x] 3.2 Wire `--node-npm` into whole-project build-context handling, the generated `.dockerignore`, and `--branch` asset-project selection like `--python-uv`; verify with tests that the ignore file is written and dry-run writes nothing.
- [ ] 3.3 Build a sample npm + Playwright project image with Docker and run a headless Playwright test offline inside it; verify the test passes with the firewall enabled.

## 4. Runtime node_modules and shm

- [ ] 4.1 Emit the `node_modules` tmpfs mount (with the options chosen in 1.1) and `--shm-size` (default `2g` under `--node-npm`) in `container_cli.py` for Docker, Podman, and Apple container; create the host mount target only if 1.1 showed it is required; verify with argv-construction tests per runtime and a dry-run output test.
- [ ] 4.2 Extend `entrypoint.sh.j2` to copy `/opt/node-project/node_modules` into `/workspace/node_modules` only when it is an empty mount point (plus the sudoers helper if 1.1 requires it); verify with a rendered-template test and a manual session showing the host `node_modules` is untouched.
- [ ] 4.3 Run an end-to-end session on Docker and on Apple container: `npm run build` with Vite writes `.vite` caches, `npx playwright test` passes, and a second session sees fresh dependencies; verify by recording results in the change notes.

## 5. Documentation

- [x] 5.1 Document `--node-npm` and `--shm-size` in `docs/usage.md`: supported repo shapes (npm only, committed lockfile, single root `node_modules`, npm workspaces), lifecycle-script caveats, the tmpfs memory trade-off, and ephemeral `node_modules`; add a short README pointer. Verify the documented commands run as written with `--dry-run`.
- [x] 5.2 Document Chromium `--no-sandbox`, install-script supply-chain considerations, and generated image contents in `docs/security.md`; verify by review against design.md risks.
- [x] 5.3 Add firewall guidance and example `--internet-proxy` configurations for web tests that need external hosts to `docs/internet-proxy.md`; verify the examples with `--dry-run`.
- [x] 5.4 Add out-of-scope follow-ups (pnpm/yarn, devcontainer support, volume-cached `node_modules`, Puppeteer/Cypress, CJK fonts) to TODO.md.

## 6. Integration

- [x] 6.1 Run `uv run python -m compileall src tests` and `uv run pytest -q`; verify both succeed.

## Implementation notes

- 4.1 and 4.2 are implemented and unit-tested, but stay open until 1.1 is done. Docker/Podman use `--tmpfs /workspace/node_modules:rw,exec,mode=1777`; Apple container uses a bare `--tmpfs /workspace/node_modules` and `--shm-size`, neither verified yet. The CLI does not create a host `node_modules` mount target. The entrypoint copies only into an empty mount whose `stat -f` type is `tmpfs`, and fails the session if the copy fails (for example when the tmpfs is not agent-writable). There is no sudoers helper yet.
- 1.1, 3.3, and 4.3 need macOS with Apple container and/or a Docker host with network access; the implementation environment had neither.
- Base image chosen for the design's open question: `debian:trixie-slim`. Confirm during 3.3 that Playwright's `install --with-deps` supports it on arm64.
