# Spec Delta

## Purpose

Let agents work offline on npm-based web projects with headless browser testing by baking npm dependencies, Chromium, and fonts into a generated sandbox image and exposing a Linux-native, writable `node_modules` at runtime.

## ADDED Requirements

### Requirement: --node-npm flag validation
The system SHALL provide a `--node-npm` flag that synthesises the sandbox base Dockerfile. The flag MUST be rejected when combined with `base_image`, `--dockerfile`, `--python-uv`, `--rust-cargo`, or `--runtime chroot`.

#### Scenario: Mutually exclusive with other build sources
- **WHEN** the user passes `--node-npm` together with `base_image`, `--dockerfile`, `--python-uv`, or `--rust-cargo`
- **THEN** the command exits with an error naming both conflicting options and builds nothing

#### Scenario: Rejected with chroot runtime
- **WHEN** the user passes `--node-npm --runtime chroot`
- **THEN** the command exits with an error stating that `--node-npm` requires an image-based runtime

### Requirement: npm manifests and lockfile are mandatory
With `--node-npm`, the system SHALL require `package.json` and one of `package-lock.json` or `npm-shrinkwrap.json` at the project root, and MUST fail before building when either is missing.

#### Scenario: Missing lockfile
- **WHEN** the project has `package.json` but neither `package-lock.json` nor `npm-shrinkwrap.json`
- **THEN** the command exits with an error explaining that `--node-npm` requires a committed npm lockfile, and no Dockerfile is written

#### Scenario: Missing package.json
- **WHEN** the project has no `package.json`
- **THEN** the command exits with an error and no Dockerfile is written

#### Scenario: Shrinkwrap accepted
- **WHEN** the project has `package.json` and `npm-shrinkwrap.json` but no `package-lock.json`
- **THEN** the build proceeds using the shrinkwrap file

### Requirement: Dependencies are installed at image build time
The generated image SHALL install project dependencies with `npm ci` from the project manifests and lockfile only, outside `/workspace`, using the sandbox's own Node.js. A failing `npm ci` MUST fail the image build. Source edits that do not touch the manifests or lockfile MUST NOT invalidate the dependency layer.

#### Scenario: Dependency layer caching
- **WHEN** only non-manifest source files change between two builds
- **THEN** the dependency install layer is reused from cache

#### Scenario: Broken lockfile fails the build
- **WHEN** `npm ci` fails during the image build (for example a lockfile out of sync with `package.json`, or a lifecycle script error)
- **THEN** the image build fails and the error is surfaced to the user

#### Scenario: npm workspaces
- **WHEN** the root `package.json` declares `workspaces`
- **THEN** every matching member `package.json` is available to `npm ci`, and member links in `node_modules` resolve to the member directories under `/workspace` at runtime

#### Scenario: Host node_modules not used as build input
- **WHEN** the host project contains a `node_modules` directory
- **THEN** it is excluded from the image build and does not influence the installed dependencies

### Requirement: Headless browser support is baked into the image
The generated image SHALL include Debian Chromium, fontconfig, `fonts-liberation`, and `fonts-noto-color-emoji`. When the lockfile contains `playwright` or `@playwright/test`, the image SHALL also include the Playwright Chromium build matching the project's locked Playwright version, installed with its system dependencies. All browser directories MUST be readable and writable by the agent user.

#### Scenario: Project without Playwright
- **WHEN** the lockfile contains neither `playwright` nor `@playwright/test`
- **THEN** the image contains system Chromium and fonts, and no Playwright browser download occurs

#### Scenario: Project with Playwright
- **WHEN** the lockfile contains `@playwright/test`
- **THEN** a headless Playwright Chromium test runs inside the sandbox with no network access and without downloading a browser

#### Scenario: Browser environment variables
- **WHEN** a `--node-npm` session starts
- **THEN** `CHROME_BIN` and `PUPPETEER_EXECUTABLE_PATH` point at the system Chromium, `PLAYWRIGHT_BROWSERS_PATH` points at the baked Playwright browser directory, and `CI` is not set by the sandbox

### Requirement: Writable Linux-native node_modules at runtime
For `--node-npm` sessions on Docker, Podman, and Apple container, the system SHALL mount an ephemeral in-memory filesystem at `/workspace/node_modules` and populate it from the image's installed dependencies before the agent starts. The mounted directory MUST be writable and executable by the agent user, MUST hide the host's `node_modules`, and writes to it MUST NOT reach the host filesystem.

#### Scenario: Host node_modules hidden
- **WHEN** the host project has a `node_modules` directory containing host-platform native binaries
- **THEN** inside the sandbox `/workspace/node_modules` contains the image's Linux dependencies and the host directory's contents are not visible

#### Scenario: Tool caches writable
- **WHEN** a tool inside the sandbox writes to `/workspace/node_modules/.vite` or `/workspace/node_modules/.cache`
- **THEN** the write succeeds and the host's `node_modules` is unchanged after the session

#### Scenario: Binaries executable
- **WHEN** the agent runs `npx <tool>` or an npm script that invokes a binary from `node_modules/.bin` or a native addon
- **THEN** it executes without permission errors

#### Scenario: Fresh copy per session
- **WHEN** a second session starts after a first session modified `/workspace/node_modules`
- **THEN** the second session sees the image's original dependencies

#### Scenario: No mount, no copy
- **WHEN** the entrypoint runs and `/workspace/node_modules` is not a sandbox-provided mount
- **THEN** the entrypoint does not copy dependencies, so it never writes into a host directory

### Requirement: Configurable shared memory size
The system SHALL accept a `--shm-size SIZE` flag and pass it to Docker, Podman, and Apple container as the container's `/dev/shm` size. For `--node-npm` sessions the default SHALL be `2g`; for other sessions the runtime's default applies unless `--shm-size` is given. `--shm-size` MUST be rejected with `--runtime chroot`.

#### Scenario: Default for --node-npm
- **WHEN** the user runs `--node-npm` without `--shm-size` on an image-based runtime
- **THEN** the container run command includes a shared memory size of `2g`

#### Scenario: Explicit override
- **WHEN** the user passes `--shm-size 4g`
- **THEN** the container run command uses `4g`

#### Scenario: Dry-run shows runtime flags
- **WHEN** the user runs `--node-npm --dry-run`
- **THEN** the printed run command includes the shared memory size and the `node_modules` mount, and no files are written and no container is started
