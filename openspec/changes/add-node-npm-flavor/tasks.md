# Tasks

## 1. Apple container spike

- [x] 1.1 On macOS with Apple container, run a Debian image as UID 1000 with `--tmpfs /workspace/node_modules` over a bind-mounted `/workspace` and `--shm-size 2g`; record in design.md whether the tmpfs is writable by UID 1000, whether it allows exec, whether a missing mount target is rejected, and the observed `/dev/shm` size (`df -h /dev/shm`). Verify by updating design.md D2 with the findings and the chosen permission approach (mount option vs sudoers helper).

## 2. CLI flags and validation

- [x] 2.1 Add `--node-npm` and `--shm-size` to `build_parser`, and extend `_resolve_build_source` with mutual-exclusion checks against `base_image`, `--dockerfile`, `--python-uv`, and `--rust-cargo`; verify with `tests/test_cli.py` cases for each conflict.
- [x] 2.2 Reject `--node-npm` and `--shm-size` with `--runtime chroot`, and validate the `--shm-size` format; verify with tests for chroot rejection and valid/invalid sizes.
- [x] 2.3 Fail when `package.json` or both lockfiles are missing, accepting `npm-shrinkwrap.json` as an alternative; verify with tests asserting the error message and that no Dockerfile is written.
- [x] 2.4 Discover resolved local package manifests from the effective version 2 or 3 npm lockfile (array and `{packages}` declarations, braces, extglobs, exclusion/re-inclusion); reject stale declarations, missing manifests and escaping paths, including during dry-run. Verify with unit tests and an optional real-npm offline compatibility test.
- [x] 2.5 Detect `playwright` / `@playwright/test` in the lockfile `packages` map; verify with tests for present, absent, and nested-dependency-only cases.

## 3. Generated Dockerfile

- [x] 3.1 Add `render_node_npm_dockerfile` producing the Debian base, Chromium and font packages, browser env vars, manifests-only `npm ci` layer in `/opt/node-project`, conditional Playwright install, agent ownership of `/opt/node-project` and `/opt/ms-playwright`, and `WORKDIR /workspace`; verify with `tests/test_renderers.py` asserting layer contents and ordering for plain, workspace, and Playwright projects.
- [x] 3.2 Wire `--node-npm` into whole-project build-context handling, the generated `.dockerignore`, and `--branch` asset-project selection like `--python-uv`; verify with tests that the ignore file is written and dry-run writes nothing.
- [x] 3.3 Build a sample npm + Playwright project image with Docker and run a headless Playwright test offline inside it; verify the test passes with the firewall enabled.

## 4. Runtime node_modules and shm

- [x] 4.1 Emit the `node_modules` tmpfs mount (with the options chosen in 1.1) and `--shm-size` (default `2g` under `--node-npm`) in `container_cli.py` for Docker, Podman, and Apple container; create the host mount target only if 1.1 showed it is required; verify with argv-construction tests per runtime and a dry-run output test.
- [x] 4.2 Extend `entrypoint.sh.j2` to require a separate tmpfs mount, reject symlinks, fail before copying on invalid mounts, and copy only into an empty valid mount; verify with rendered-shell regression tests and the user-reported end-to-end host-isolation checks. No sudoers helper is required by the validated flow.
- [x] 4.3 Validate the Node/npm sandbox with a real application repository, including startup and headless browser testing with Playwright explicitly launching `/usr/bin/chromium`; record the user-reported validation and resulting fixes in the change notes.
- [x] 4.4 Complete the runtime-specific end-to-end matrix on Docker and Apple container: `npm run build` with Vite writes `.vite` caches, `npx playwright test` passes with the firewall enabled, host `node_modules` stays untouched, and a second session sees fresh dependencies; record the runtime and results for each check in the change notes.

## 5. Documentation

- [x] 5.1 Document `--node-npm` and `--shm-size` in `docs/usage.md`: supported repo shapes (npm only, committed lockfile, single root `node_modules`, npm workspaces), lifecycle-script caveats, the tmpfs memory trade-off, and ephemeral `node_modules`; add a short README pointer. Verify the documented commands run as written with `--dry-run`.
- [x] 5.2 Document Chromium `--no-sandbox`, install-script supply-chain considerations, and generated image contents in `docs/security.md`; verify by review against design.md risks.
- [x] 5.3 Add firewall guidance and example `--internet-proxy` configurations for web tests that need external hosts to `docs/internet-proxy.md`; verify the examples with `--dry-run`.
- [x] 5.4 Add out-of-scope follow-ups (pnpm/yarn, devcontainer support, volume-cached `node_modules`, Puppeteer/Cypress, CJK fonts) to TODO.md.

## 6. Integration

- [x] 6.1 Run `uv run python -m compileall src tests` and `uv run pytest -q`; verify both succeed.

## Implementation notes

- The user confirmed on 2026-10-02 that Docker and Apple container e2e checks passed and requested that these checks be marked complete. Tasks 1.1, 3.3, 4.1, 4.2, and 4.4 are recorded complete on that basis. These are user-reported results; raw logs, runtime versions, mount permission measurements, and `/dev/shm` measurements were not supplied.
- Docker/Podman keep `--tmpfs /workspace/node_modules:rw,exec,mode=1777`; Apple container keeps `--tmpfs /workspace/node_modules`. The CLI does not create a host mount target or add a sudoers helper. Podman has argv-construction coverage but no reported e2e result.
- The base image is `debian:trixie-slim`; the earlier base-image choice is resolved. The smoke script still needs stronger shared-memory assertions, Vite build/cache coverage, and bundled-browser coverage; these follow-ups are tracked in `TODO.md`.
- Review fixes (2026-10-03): use npm's locked local package resolution instead of Python glob expansion, validate workspace metadata during dry-run, and require a separate tmpfs mount before dependency copying. Regression tests cover npm pattern compatibility, manifest/path validation, and host directories on tmpfs. The earlier e2e confirmation applies to the feature before these review fixes; verification of the fixes is recorded separately.

## Review-fix verification (2026-10-03)

- `uv run pytest -q`: 694 passed, 4 skipped, 183 subtests passed. This includes real npm offline workspace installs and a copy-guard regression using an ordinary directory on `/dev/shm`.
- Python compilation, Ruff checks on the changed Python files, rendered entrypoint shell syntax, and smoke-script shell syntax passed.
- A direct CLI dry-run against a real npm-generated workspace lockfile succeeded and left the project filesystem unchanged.
- Docker and Apple container e2e checks were not rerun in this environment after the review fixes; their previously confirmed results remain recorded above.

## Real-repository validation (2026-10-02)

- The user confirmed that the sandbox starts after the dependency-copy fix and subsequently reported testing it with a real repository.
- Fresh projects with no dependencies needed the image build to create an empty `node_modules` after successful `npm ci`.
- Copying the source directory itself with `cp -a` failed when preserving timestamps on the root-owned tmpfs. The entrypoint now copies only its immediate contents with `find` and `cp -a`, retaining package metadata and symlinks without modifying the mount root's metadata.
- The user's end-to-end test showed the documented default Playwright configuration did not select system Chromium. The setup guide and copyable application `AGENTS.md` now explicitly require `executablePath: '/usr/bin/chromium'` and `args: ['--no-sandbox']`.
- Setup, repeatable tests, prompts, and application instructions are documented in `docs/headless-browser-testing.md`.
