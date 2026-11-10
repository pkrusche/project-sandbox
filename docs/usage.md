# Usage Guide

## Install

Install the command from PyPI with uv:

```bash
uv tool install project-sandbox
project-sandbox --help
```

To run the PyPI package without installing it:

```bash
uvx project-sandbox --help
uvx project-sandbox /absolute/path/to/repo python:3.12-slim
```

From a source checkout:

```bash
uv sync
uv run project-sandbox --help
```

Check that a container runtime, git, and (optionally) jj are ready:

```bash
uv run project-sandbox doctor
```

See [Host prerequisite checks](doctor.md) for what is probed.

## Quick Start

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

To build on top of a repo's existing Dockerfile instead of a base image tag:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

In this mode, the generated `Dockerfile` creates a dependency stage for the
sandbox's apt tooling, Node.js, jj, OpenSpec, and coding agents. The source
Dockerfile's final stage inherits that dependency stage before its original
instructions are appended. This keeps the expensive, source-independent layers
cacheable when project files change and preserves earlier build stages and
`COPY --from` references.

For proxies and CAs needed during image construction, use
[`--build-proxy URL`, `--build-no-proxy HOSTS`, and `--build-ca-cert`](build-network.md).
These configure access before build commands and leave runtime firewall
permissions unchanged.
For custom prerequisites such as package mirrors, put setup in a stage named
`prefix` and make the final stage inherit from it (directly or through other
named stages):

```dockerfile
FROM python:3.12-slim AS prefix
COPY proxy-ca.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV HTTPS_PROXY=http://proxy.example

FROM prefix AS final
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
```

The name `prefix` is recognized case-insensitively. It must be unique and an
ancestor of the final stage; otherwise rendering fails rather than silently
running sandbox downloads without the declared prerequisites. The sandbox
inserts its dependency stage immediately after `prefix` and carries it along the
final stage's inheritance path. Other build branches remain unchanged.

Inserting the dependency stage after `prefix` shifts the positional index of
every stage declared after it, so a numeric `--from=<N>` (or
`--mount=...,from=<N>`) reference to one of those stages would silently point
at the wrong stage. Rendering fails with an error instead; use a named stage
(`AS <name>` / `--from=<name>`) for anything declared after `prefix`.

Postfix source instructions run as root and may assume sandbox-installed tools
such as `git` and `curl` are available, but they run before the sandbox creates
the `agent` user, its home directory, or the `/project-sandbox-config` and
`/project-sandbox-secrets` paths. The sandbox adds that user/config setup and its
firewall and entrypoint after the source content.

The build context defaults to the project root so existing `COPY` instructions
keep working; use `--docker-context` if that Dockerfile expects a different
context. If the source Dockerfile defines its own non-root user or
standalone UID/GID setup, project-sandbox removes those instructions and prints a
warning. It creates its own `agent` user, matching the host UID/GID for direct
Docker/Podman runs on Linux and defaulting to UID/GID 1000 elsewhere. If a
`RUN` instruction mixes user-management commands with unrelated build steps,
project-sandbox rejects it instead of silently dropping the unrelated work.

Use `--dry-run` to preview every action without writing files or starting the
runtime:

```bash
uv run project-sandbox --dry-run /absolute/path/to/repo python:3.12-slim
```

Direct CLI runs use `--runtime auto` by default. Auto-selection prefers Apple
`container` on macOS, and Docker then Podman on Linux. Override it when needed:

```bash
uv run project-sandbox --runtime docker --agent bash /absolute/path/to/repo python:3.12-slim
uv run project-sandbox --runtime podman --agent bash /absolute/path/to/repo python:3.12-slim
uv run project-sandbox --runtime apple-container --agent bash /absolute/path/to/repo python:3.12-slim
```

With pasta (the default on Linux), Podman builds and sessions use their own
address, `198.18.0.2/24`, so proxies and gateways on the host's own LAN address
stay reachable; see `docs/security.md`.

Podman must run rootless: `--runtime podman` (and `auto` when it picks Podman)
refuses to start when `podman info` reports a rootful setup, such as running
project-sandbox as root or a Podman machine set with `--rootful`. Podman runs
pass `--userns=keep-id` so the sandbox user, built with your host UID, can
write the bind-mounted workspace.

Linux also provides an explicit layout-inspection mode that needs no image:

```bash
uv run project-sandbox --runtime chroot --agent bash /absolute/path/to/repo
```

This opens Bash in a rootless chroot mirroring the normal bind-mount layout.
Headless Bash via `--prompt` or `--prompt-text` is also supported. It does not
install or run coding-agent CLIs and is not an isolation or security boundary.

## API Key Injection

`--no-forward-credentials` starts a direct agent container without staging or
mounting host agent credential files. For API-key based providers, inject only
the specific environment variables the session needs:

```bash
export ANTHROPIC_API_KEY=...

uv run project-sandbox \
  --no-forward-credentials \
  --api-key-env ANTHROPIC_API_KEY \
  --agent bash \
  /absolute/path/to/repo \
  python:3.12-slim
```

You can also load a dotenv-style file containing `KEY=VALUE` lines:

```bash
uv run project-sandbox \
  --no-forward-credentials \
  --api-key-env-file /absolute/path/to/.env.sandbox \
  --agent bash \
  /absolute/path/to/repo \
  python:3.12-slim
```

Repeat `--api-key-env` or `--api-key-env-file` to inject multiple keys. Dry-runs
validate the variables but print `<redacted>` instead of secret values.
Environment-based secrets can appear in container runtime metadata or logs, so
prefer this only for explicit API-key sessions where mounted agent credentials
are intentionally disabled.

## GitHub Copilot

`--agent copilot` runs the [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli)
(`@github/copilot`, pinned in the image) on your Copilot subscription:

```bash
uv run project-sandbox --agent copilot /absolute/path/to/repo python:3.12-slim
uv run project-sandbox --agent copilot --model gpt-5-mini --effort high \
  --prompt-text "Summarize this repository" /absolute/path/to/repo python:3.12-slim
```

Copilot runs only on a Copilot subscription: `--agent-proxy` and `--fast` are
rejected.

### Host

`--copilot-host HOST` picks the GitHub host that issues the token:

- `github.com` (default) covers personal, Copilot Business, and Copilot
  Enterprise accounts on GitHub.com (GitHub Enterprise Cloud). No flag is needed.
- `<tenant>.ghe.com` covers GitHub Enterprise Cloud with data residency, for
  example `--copilot-host acme.ghe.com`. The container then sets
  `COPILOT_GH_HOST`, and the firewall also pins the tenant's hosts.

Any other host, including self-hosted GitHub Enterprise Server, is rejected.

### Token

The token is resolved on the host at every launch, in this order:

1. `pass show <entry>`. The default entry is `copilot-github-token` for
   github.com and `copilot-github-token/<host>` for a tenant, so a github.com
   token is never sent to a tenant. `--copilot-token-pass ENTRY` names a
   different entry; an explicitly named entry that fails is an error and never
   falls back to `gh`.
2. `gh auth token --hostname <host>`. This GitHub CLI token is often broadly
   scoped (for example `repo` and `workflow`), so every run that uses it prints
   a `[W]` warning; see [Security](security.md#github-copilot-token).
3. Otherwise the CLI exits and names both remedies.

The recommended setup is a fine-grained personal access token with only the
**Copilot Requests** permission (Settings → Developer settings → Fine-grained
tokens; for a tenant, create it on the tenant host). Store it in pass:

```bash
pass insert copilot-github-token                  # github.com
pass insert copilot-github-token/acme.ghe.com     # a GHE.com tenant
```

Only the first line of the entry is used. Classic `ghp_` tokens are rejected
because Copilot does not accept them. To use the GitHub CLI fallback instead,
run `gh auth login --hostname <host>` once.

The token is written to a mode-0600 file in the session's private staging
(`secrets/copilot/` under the runtime root, never into the project; see
[Where state lives](#where-state-lives)), mounted read-only at
`/project-sandbox-secrets/copilot` for Copilot sessions only, and deleted with
the session's staging when it ends. The entrypoint exports it as `COPILOT_GITHUB_TOKEN` just for
the Copilot process. `--no-forward-credentials` skips the lookup entirely;
Copilot then starts unauthenticated and you can log in inside the sandbox.

`--dry-run` shows the planned token source, mount, environment, and firewall
additions without calling `pass` or `gh`:

```bash
uv run project-sandbox --dry-run --agent copilot /absolute/path/to/repo python:3.12-slim
uv run project-sandbox --dry-run --agent copilot --copilot-host acme.ghe.com \
  --prompt-text "hi" /absolute/path/to/repo python:3.12-slim
```

### Behaviour in the sandbox

- `--agent copilot` implies `--allow-github` (see the firewall notes in
  [Security](security.md#network-firewall)).
- `$COPILOT_HOME` is `/home/agent/.copilot`, seeded with a `config.json` whose
  `trusted_folders` lists `/workspace`, so no folder-trust prompt appears.
  `COPILOT_AUTO_UPDATE=false` and `--no-auto-update` keep the pinned version.
- Interactive runs execute `copilot --allow-all --no-auto-update
  --disable-builtin-mcps`; headless runs add `-p "$PROMPT" --no-ask-user
  --output-format json`. The built-in GitHub MCP server is disabled because it
  would act on GitHub with the same token.
- `--model` and `--effort` become Copilot's own `--model` and `--effort` flags.

Migrating from `--agent bash --prompt-text 'copilot …'`: that form no longer
opens GitHub network access (the prompt heuristic was removed) and never
receives a Copilot token. Use `--agent copilot --prompt-text '…'` instead.

## Python + uv Projects

For a Python project managed with uv, create a `Dockerfile` at the project root
that installs uv and pre-populates the package cache so the agent can run
`uv sync` / `uv run` inside the sandbox without reaching PyPI, which the firewall
blocks at runtime:

```dockerfile
FROM python:3.11-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000

COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 /uv /usr/local/bin/uv

ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN install -d -o "${AGENT_UID}" -g "${AGENT_GID}" /opt/uv-cache /opt/venv /opt/uv-python

# Pre-populate the uv package cache with all project dependencies so the agent
# can run `uv sync` / `uv run` inside the sandbox without reaching PyPI.
# Two layers: the deps-only layer only rebuilds when pyproject.toml/uv.lock
# change, while the slower project-install layer rebuilds on every source edit
# but reuses the dependency cache already populated above.
COPY pyproject.toml uv.lock /tmp/project-setup/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --no-install-project \
        --project /tmp/project-setup

# README.md is declared as `readme =` in pyproject.toml, so the build backend
# needs it present once the project itself is installed below.
COPY README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
# Match ownership to the agent user created by the sandbox layers that follow.
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R "${AGENT_UID}:${AGENT_GID}" /opt/uv-cache /opt/venv /opt/uv-python \
    && rm -rf /tmp/project-setup
```

Key points:

- The first layer only copies `pyproject.toml` and `uv.lock`, and installs
  dependencies with `--no-install-project` so it doesn't need the project's
  source at all. It only rebuilds when the manifest/lockfile change.
- The second layer copies all files referenced by `pyproject.toml` metadata
  (including `README.md`, since it's declared as `readme =`) plus `src/`, then
  runs `uv sync` again (without `--no-install-project`) to build and install
  the local project itself. This layer rebuilds on every source edit, but
  reuses the dependency cache the first layer already populated.
- The `chown -R 1000:1000` gives the sandbox's `agent` user, created by the
  layers that follow, full access to the pre-warmed cache, virtual environment,
  and managed Python directory.
- `ENV UV_CACHE_DIR=/opt/uv-cache` persists into the running container so the
  agent's `uv sync` reads from the cache instead of PyPI.

**Custom `--dockerfile` builds must configure uv environment isolation explicitly.**
The project directory is mounted at `/workspace`, including the host's `.venv`.
Without `UV_PROJECT_ENVIRONMENT=/opt/venv`, sandbox `uv run` or `uv sync` can
replace that shared `.venv` with a Linux environment, and host uv commands can
replace it again. A typical symptom is `Ignoring existing virtual environment
linked to non-existent Python interpreter`, followed by uv removing `.venv`.
Recreating it may then fail because the sandbox is offline and the required
platform-specific packages are absent from its cache.

Set the environment variables shown above before the build-time `uv sync`
commands. `UV_PYTHON_INSTALL_DIR` also keeps downloaded interpreters inside the
container and accessible to the agent. Rebuild and recreate an existing sandbox
to apply the settings. The generated `--python-uv` Dockerfile sets them automatically.

Generated Codex configurations allow `UV_CACHE_DIR`, `UV_OFFLINE`,
`UV_PROJECT_ENVIRONMENT`, and `UV_PYTHON_INSTALL_DIR` through
to tool shells alongside core shell environment variables. Their values come
from the running container; unrelated environment variables are filtered out.
Restart with regenerated configuration to apply this policy to existing sessions.

Then run:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

## Node.js + npm Projects

For npm-based web projects, `--node-npm` synthesises the base Dockerfile
(`Dockerfile.node-npm`, rendered into private staging) instead of you writing
one:

```bash
uv run project-sandbox --node-npm --agent claude /absolute/path/to/repo
```

The image bakes in the project's dependencies and Chromium so any supported
agent or Bash shell can build and run browser tests with the runtime firewall
enabled. Choose the agent independently with `--agent`.

For a Playwright setup and reusable repository instructions,
see [Headless browser testing](headless-browser-testing.md).

- **Dependencies:** `npm ci` runs at image build time from `package.json`, the
  lockfile, and npm workspace member `package.json` files only, into
  `/opt/node-project/node_modules`. Source edits do not invalidate this layer;
  manifest or lockfile changes do. A failing `npm ci` fails the build.
- **Browsers:** Debian `chromium`, fontconfig, `fonts-liberation`, and
  `fonts-noto-color-emoji` are always installed. When the lockfile contains
  `playwright` or `@playwright/test`, the project's own Playwright CLI installs
  its matching Chromium (with system dependencies) into `/opt/ms-playwright`.
- **Environment:** `CHROME_BIN` and `PUPPETEER_EXECUTABLE_PATH` point at
  `/usr/bin/chromium`, `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, and
  `PUPPETEER_SKIP_DOWNLOAD=true`. `CI` is not set.
- **Node.js:** the sandbox's pinned Node.js (v26) and its bundled npm. There is
  no per-project Node version selection and no corepack.

Supported repo shapes:

- npm only, with `package.json` and a committed `package-lock.json` or
  `npm-shrinkwrap.json` at the project root. The CLI fails if either is missing.
- A single root `node_modules`, including npm workspaces (`workspaces` as an
  array or `{"packages": [...]}`). Workspace manifests are selected from npm's
  resolved local package paths in a version 2 or 3 lockfile, so npm's brace
  patterns, extglobs, and exclusion/re-inclusion rules work without host npm
  being required by project-sandbox. Regenerate and commit the lockfile when
  adding, removing, or changing workspaces, including changes to directories
  matched by existing globs. Stale root declarations, missing manifests, and
  local package paths outside the project are rejected before building, also
  during dry-run. `npm-shrinkwrap.json` takes precedence when both lockfiles
  exist. Plain projects without workspaces can still use a version 1 lockfile.
  Layouts that need per-package `node_modules` directories, pnpm, yarn, and bun
  are not supported.
- Only the manifests and lockfile enter the dependency layer, so a root
  `prepare`/`postinstall` lifecycle script that needs source files fails the
  build; move such steps into a separate npm script. `.npmrc` is not copied, so
  private registry configuration and registry auth are not available at build
  time. This also excludes public-registry install settings such as
  `legacy-peer-deps` and `install-links`: npm requires the same tree-shaping
  options used to create the lockfile for `npm ci`. Projects requiring those
  options should use a custom Dockerfile; do not copy registry credentials into
  the image. See [npm ci](https://docs.npmjs.com/cli/commands/npm-ci/).

At runtime `/workspace/node_modules` is an in-memory tmpfs that the entrypoint
fills from the image before the agent starts:

- It hides the host's `node_modules` (whose native binaries target the host
  platform) and writes never reach the host.
- The entrypoint requires the destination to be a separate tmpfs mount, not a
  symlink or an ordinary directory on a tmpfs-backed host filesystem. Without
  that mount it fails before copying dependencies or starting the agent.
- It is writable and executable, so `node_modules/.bin` tools, native addons,
  and tool caches such as `.vite` and `.cache` work.
- It is ephemeral: every session starts from the image's dependencies. To add a
  dependency, update `package.json` and the lockfile on the host (or let the
  agent edit them and rebuild), then start a new session; the image rebuilds the
  dependency layer.
- It costs memory. `node_modules` and `/dev/shm` both count against `--memory`
  (default `8g`). The build prints the baked `node_modules` size (`du -sh`);
  raise `--memory` for large dependency trees.

`--shm-size SIZE` sets the container's `/dev/shm` size (`<number>` with a `b`,
`k`, `m`, or `g` suffix). It defaults to `2g` under `--node-npm`, because
Chromium crashes with the runtime's small default, and otherwise leaves the
runtime default alone. Both flags are rejected with `--runtime chroot`.

```bash
uv run project-sandbox --node-npm --shm-size 4g --memory 12g --agent bash /absolute/path/to/repo
```

Chromium needs `--no-sandbox` inside the container (Playwright passes it by
default; configure Puppeteer or Karma launchers to do the same); see
[security](security.md#nodejs--npm-images---node-npm). For web tests that need
external hosts, see [Internet proxy](internet-proxy.md#web-tests-that-need-external-hosts).

In an isolated session, `--node-npm` builds the image from the session
directory, like `--python-uv`.

Generated Node/npm devcontainers are not supported yet: their config lacks the
`node_modules` tmpfs and `--shm-size`, so the dependencies baked into the image
are not available at `/workspace/node_modules`. Use a direct CLI session instead.

## Session observability and image warm-up

Every direct container run reports a session ID and a container name derived
from it. Session records survive CLI restarts in
`$XDG_STATE_HOME/project-sandbox/sessions/` (defaulting to
`~/.local/state/project-sandbox/sessions/`). List them for an orchestrator with:

```bash
uv run project-sandbox sessions list --json
```

A record's `status` is `running`, `completed`, `rate_limited`, `interrupted`
(the CLI was interrupted or failed on its way out), or `orphaned` (still marked
running, but its CLI process is gone). `container_name` can be matched against
the selected runtime's container listing. Sessions that end on an HTTP 429 are
marked `rate_limited` and return exit code 75 (`EX_TEMPFAIL`), while timeouts
continue to return 124. Only the end of the session log is classified, so a
transient 429 the agent retried through does not mark an unrelated later failure
as rate limited.

Warm the image cache without starting an agent or container with:

```bash
uv run project-sandbox /path/to/project --build-only
```

The usual image source, runtime, cache, `--force-build`, and resource options
apply. `--dry-run --build-only` previews the build without writing files.

## Devcontainer Flow

The generated devcontainer is a place to run and debug the project's tests with
all of its dependencies installed. It uses the same base image and dependency
layers as a direct session, but **no agent runs there**: it has no agent CLIs,
credentials, agent configs or agent IDE extensions. To run an agent, start a
direct session (`project-sandbox --agent …`) or use the TUI.

Render it explicitly, with the same build options you use for sessions:

```bash
uv run project-sandbox /absolute/path/to/repo --devcontainer --python-uv
uv run project-sandbox /absolute/path/to/repo --devcontainer python:3.12-slim
```

`--devcontainer` writes `.devcontainer/devcontainer.json`, its `Dockerfile` and
the identity init script as real files in `.devcontainer/`, with a
`.devcontainer/.gitignore` of `*` so the directory ignores itself (the
project's root `.gitignore` is not touched). A symlinked `.devcontainer` is
refused, and a symlinked file in it is replaced, never followed. It prints
where it wrote and the network mode, and exits. It builds no image, stages no credentials and
starts no container. `--devcontainer --dry-run` lists the files it would write
and the network mode, and writes nothing. Then open the project in VS Code (or
another devcontainer-aware IDE) and choose **Reopen in Container**. Re-run
`--devcontainer` after changing build options, and rebuild the container in
the IDE.

Ordinary launches never write or change devcontainer files. They only create
`.devcontainer/` (holding just its `.gitignore`) when it is missing, so the
session can mask it.
`--devcontainer` can't be combined with `--agent`, a prompt, `--branch`,
`--build-only` or `--runtime chroot`. It ignores `--egress`.

### Network

By default the devcontainer has **no network**: `runArgs` contains
`--network none`, so only loopback works. Servers a test starts on `127.0.0.1`
are reachable. Outbound connections, including to services on the host, fail
immediately. The offline switches of the generated toolchains are set
(`UV_OFFLINE=1`, `CARGO_NET_OFFLINE=true`, `npm_config_offline=true`), so tools
fail fast instead of timing out. Building the image is not affected: the IDE
builds with normal network access.

If your IDE or tests need a network, opt in:

```bash
uv run project-sandbox /absolute/path/to/repo --devcontainer \
  --devcontainer-network=runtime --python-uv
```

`--devcontainer-network=runtime` gives the devcontainer the runtime's default
network, **unfiltered**: there is no firewall, no egress policy and no added
capability, and the command prints a warning saying so. The offline switches
are not set. With `--internet-proxy URL`, its proxy environment is applied.
`--devcontainer-network=none` is the default. The flag is only valid with
`--devcontainer`.

### Privileges and masks

The devcontainer runs as `agent` with no added capabilities and no sudoers
rule. Its `postStartCommand` applies your git/jj identity and runs nothing as
root. `/workspace/.devcontainer` (and a legacy `/workspace/.project-sandbox`,
when present) is masked with an empty read-only directory from the project's
private state, so test code (which may come from an agent session) cannot
change `devcontainer.json`, whose `initializeCommand` runs on the host. Bash
history persists across rebuilds in the project's private state, and
`--mount`, `--memory` and `--cpus` apply.

### IDE compatibility

- **VS Code** (Dev Containers extension) works offline. It downloads its server
  and extensions on the host and copies them in. Tested with VS Code 1.145 and
  Dev Containers 0.463 on Docker Desktop: opening the container, installing the
  Python, debugpy and Pylance extensions, and running and debugging a pytest
  test all worked with `--network none`.
- **JetBrains** IDEs download their backend inside the container, so they
  probably can't start offline. They are not tested; use
  `--devcontainer-network=runtime`.
- **Extensions that download components inside the container at runtime**
  (for example C#/.NET or some debuggers) need `--devcontainer-network=runtime`.
- **Tests that need host services**, such as a database on the host, need
  `--devcontainer-network=runtime`.

Remote devcontainer services such as Codespaces are not supported: the
generated spec mounts history and mask directories from host state.

### Upgrading from older versions

Earlier versions rendered `.devcontainer/` on every launch, with agents,
staged credentials and a firewall. Those files stay until you run
`--devcontainer` once, which rewrites `devcontainer.json`, replaces the old
`Dockerfile` link with a real file, and removes the stale `claude*`, `codex*`
and `init-firewall.sh` links. Rebuild the container in the IDE afterwards.

## Where state lives

project-sandbox keeps everything it generates outside your project, in two
private per-user roots (directories `0700`, secret files `0600`, every path
checked to be yours and not a symbolic link before use):

- **Project state**, persistent, in
  `$XDG_STATE_HOME/project-sandbox/projects/<name>-<hash>/` (default
  `~/.local/state/...`): the image-cache fingerprint, Dockerfile checksums,
  interactive shell and Claude history, and default headless logs under
  `sessions/`. Session records stay in `$XDG_STATE_HOME/project-sandbox/sessions/`
  and host locks in `.../locks/`.
- **Session staging**, ephemeral, in `<runtime root>/staging/<session-id>/`:
  the rendered build context, agent configs, staged credentials, the Copilot
  token, `api-keys.env` and the prompt. Each session mounts only its own and
  deletes it when it exits; a later launch deletes staging left by a session
  whose process died.

The runtime root is `$PROJECT_SANDBOX_RUNTIME_DIR` when set, else
`$XDG_RUNTIME_DIR/project-sandbox` (Linux) or `$TMPDIR/project-sandbox`
(macOS) when that directory is private to you, else
`/tmp/project-sandbox-<uid>`. The container runtime must be able to bind-mount
it. Docker Desktop, Podman machine and Apple `container` mount the macOS
defaults; on Colima or Lima, which share only `$HOME` by default, point it at a
private directory under your home:

```bash
mkdir -m 700 -p ~/.cache/project-sandbox-runtime
export PROJECT_SANDBOX_RUNTIME_DIR=~/.cache/project-sandbox-runtime
```

The value must be absolute, and the directory is checked like any state
directory. macOS deletes files in `$TMPDIR` that are older than three days
(`dirhelper`, daily): for sessions that run longer, set
`PROJECT_SANDBOX_RUNTIME_DIR` as well.

`--dry-run` prints the staging and state directories it would use and creates
neither. See [Generated files](runtime.md#where-state-lives) for the full
layout.

Inside the project a launch writes only `.devcontainer/` (an empty,
self-ignoring mask target, or the files `--devcontainer` renders) and, for
whole-project builds, the secret-free build-input copy in `.project-sandbox/`.
It never edits the project's `.gitignore`; marker lines older versions added
there are left in place.

### Upgrading: migration of `.project-sandbox/`

Older versions kept all of this in `<project>/.project-sandbox/`. The first
launch of this version migrates it once, under the project's build lock:

- shell and Claude history and session logs move into the project's private
  state (regular files only; links are never followed);
- every file and directory older versions generated is deleted, including stale
  secrets (`api-keys.env`, agent-proxy configs, staged credentials), which the
  launch names;
- anything else is left in place and named;
- the project's old credential staging tree,
  `/tmp/project-sandbox-<uid>/<digest>/`, is deleted;
- a `migrated` marker stops it from running again.

While a session of the project started by an older version is still running,
the migration is postponed with a notice, because that session has those files
mounted. `--dry-run` prints the planned migration (`Would migrate: ...`) and
changes nothing. Tools that read `.project-sandbox/sessions/*.log` directly
should use the path the CLI prints, or `--log`.

## Image Builds

The container image is built before a direct-run session starts. To keep
repeated local runs fast, the build is **skipped when nothing changed**:
project-sandbox fingerprints the generated build inputs (the rendered
`Dockerfile`, `entrypoint.sh`, `project-sandbox-init`, `init-firewall.sh`, the
devcontainer init script, the base image, and the image tag) and records that
fingerprint in the project's private state (`build-state.json`; see
[Where state lives](#where-state-lives)). Every session renders its own build
directory, so concurrent launches never rewrite each other's build context; a
per-project lock serializes the cache check and the build. On the next run, if the fingerprint still
matches and the runtime confirms the image exists, the build is skipped and
`Reusing cached image (inputs unchanged)` is printed; otherwise the image is
rebuilt and the build duration is reported (`Built image in 12.3s`).

- Auto-skip applies to the default base-image flow. `--python-uv`,
  `--rust-cargo`, `--node-npm`, and `--dockerfile` builds use the whole project
  as the build context, so they always invoke the build and rely on the
  runtime's layer cache instead. Their generated, secret-free build inputs are
  copied into `.project-sandbox/` of the build context right before the build
  (a symlinked `.project-sandbox` is refused), so `--docker-context` must
  contain the project's `.project-sandbox` path.
- For the generated `--python-uv`, `--rust-cargo`, and `--node-npm` flows only,
  a generated `Dockerfile.dockerignore` (copied with the build inputs)
  trims virtualenvs, `node_modules`, and tool caches from the context (it does
  not exclude `.git` — git version backends read it during the in-image install
  — nor `.project-sandbox/`, whose scripts are copied into the image). It is not
  generated for user-supplied `--dockerfile` builds, which may legitimately copy
  those paths. If the project already has a root `.dockerignore`, it is left
  authoritative and no per-Dockerfile file is generated.
- `--force-build` rebuilds even when the cache is valid.
- `--no-build` skips the build entirely, assuming the image already exists. It
  refuses an image built before the root-stage privilege drop (no
  `org.project-sandbox.privilege-model=root-stage-v1` label); rebuild it once
  without `--no-build`.

## Version Control Modes

Every session runs in one of two VCS modes. `--vcs` picks the mode; the default
depends on the project:

| Project | no `--vcs` | `--vcs isolated` | `--vcs shared` |
|---|---|---|---|
| has `.jj/` | isolated | isolated | in place |
| `.git/` only | error, with both hints below | error, with the jj hint | in place |
| neither | in place | error | error |

A Git-only project has no isolated mode. Either add jj, which keeps your Git
history and lets you keep using `git`:

```bash
jj git init --colocate /absolute/path/to/repo
```

or run with `--vcs shared` to mount the project in place. project-sandbox
never initializes a repository itself.

`--dry-run` shows the mode, the bookmark, the start revision and the session
directory without creating anything.

### Isolated sessions (default in jj projects)

The agent works in a separate session directory, a jj workspace of your
repository next to it (`<repo>-workspaces/<bookmark>`), and never sees your
repository's `.jj` or `.git`. Before the container starts, project-sandbox
creates a private, throwaway jj and Git repository over the session's files,
with one `session base` commit. Inside the sandbox, `jj` and `git` work as
usual: the agent can diff, commit and inspect its own changes.

When the session ends, project-sandbox records everything the agent changed as
**one commit** on the session's bookmark, using only its own metadata, and then
removes the session directory and the private repository. The agent's own
commits and their messages are not kept; the result commit gets a message with
the bookmark name and the date. Your main checkout is never touched: merge,
rebase or open a pull request from the bookmark yourself.

```bash
uv run project-sandbox \
  --branch feature/login \
  --agent claude --prompt /absolute/path/to/prompt.txt \
  /absolute/path/to/repo python:3.12-slim
```

- `--branch <name>` names the bookmark. It is reused if it exists, and the
  session then starts from it. Without `--branch`, the bookmark is
  `sandbox/<session-id>`; the CLI prints it at the start and at the end, and it
  is in the `--json-summary` `bookmark` field.
- A new bookmark starts from your main working copy, including its
  uncommitted changes. If the working copy has no changes, it starts from its
  parent. `--branch-start-at <revision>` picks another start for a **new**
  bookmark, and is an error when the bookmark exists.
- A session that changed nothing leaves the bookmark at its start.
- The session directory contains only tracked files. Ignored folders, such as
  datasets or build output, are absent; see `--share` below.
- jj refuses new files larger than `snapshot.max-new-file-size`. The CLI lists
  any it refused, and keeps the session directory so you can add them yourself.
- `--keep-workspace` keeps the session directory and private repository after
  the session; a later `--branch <name>` run resumes from them, including the
  agent's private history. A failed session is always kept, after its files are
  recorded, and the CLI prints its path. A killed or timed-out container is
  treated the same way: its files are still recorded.
- `--worktree-dir <path>` places the session directories somewhere other than
  the sibling `<repo>-workspaces` directory. The private repositories go into a
  `.session-vcs/` directory next to them.

Only one session can run per bookmark. A second launch on a running bookmark
fails and names the running session, also when the first CLI was killed and
its container is still running. Sessions on different bookmarks run in
parallel.

A session directory kept by an older project-sandbox version has no private
repository and is refused, with the commands to keep its work on the bookmark
and remove it.

### Sharing ignored folders

`--share PATH` mounts the project's directory `PATH` at `/workspace/PATH` in an
isolated session, read-only. `--share PATH:rw` makes it writable. The option is
repeatable.

```bash
uv run project-sandbox --agent claude \
  --share data --share out:rw \
  /absolute/path/to/repo python:3.12-slim
```

- `PATH` is relative to the project and must be an existing directory inside
  it that the session's checkout doesn't contain, which in practice means an
  ignored one. A tracked path, a single file, a path that resolves outside the
  project, repository metadata (`.git`, `.jj`), `.devcontainer`,
  `.project-sandbox`, and agent or IDE config that host tools run (such as
  `.claude/settings.json`, `.mcp.json`, `.vscode/tasks.json`) are refused before
  the container starts.
- The agent's `jj` and `git` don't report the shared folder's files as new.
- Writes through a writable share go straight to the real folder. They are
  **not** part of the result commit, and concurrent sessions sharing the same
  folder see each other's writes. The launch lists every writable share.
- In shared mode and in projects without a repository, `--share` is accepted
  and has no effect: the whole project is mounted anyway.

### Shared sessions (`--vcs shared`)

The project is mounted in place at `/workspace`, with its real `.git` and
`.jj` writable, as sessions without `--branch` were before isolated mode. Use it
for work on history itself, such as rewriting a commit series with the agent,
or for Git-only projects.

- The agent can change everything under `.git` and `.jj`, including hooks and
  config that your host `git` and `jj` run later. Every shared launch prints a
  warning saying so. Review `.git/hooks`, `.git/config` and `.jj/repo/config.toml`
  after a shared session.
- Only one shared session runs per repository at a time; a second one fails
  and names the running session. Isolated sessions can run beside it.
- `--branch` and `--keep-workspace` are refused with `--vcs shared`.

### Notes

On macOS, container runtimes reach the host filesystem through a VM share whose
metadata cache can serve stale directory entries. Each private repository
therefore gets a directory name no earlier session used, the
`<repo>-workspaces/` and `.session-vcs/` directories are never removed, and the
CLI waits up to 1.5 s after creating new session directories before starting the
container (measured from their creation, so build time counts against it).
Linux containers never wait; `--verbose` reports the wait when one happens.

Image cache checks and first builds are serialized per generated build context,
so parallel sessions wait for and reuse an identical image instead of building
it twice.

## Unsupervised Sessions

Run the agent without a TTY, starting from a prompt and writing all output to a
log file:

```bash
uv run project-sandbox \
  --prompt /absolute/path/to/prompt.txt \
  /absolute/path/to/repo \
  python:3.12-slim
```

- `--prompt FILE` copies the file into the session's private `prompt/` staging
  directory (each session has its own),
  bind-mounts only that directory read-only, and reads the prompt from
  `/project-sandbox-prompt/<name>`.
- `--prompt-text "..."` writes the prompt to the session's private `prompt/`
  staging directory, bind-mounts that directory read-only, and reads it from
  `/project-sandbox-prompt/prompt.txt`.
- `--agent {claude,codex,opencode,pi,copilot,bash}` selects which agent to run.
  If omitted, the CLI only initializes generated config files unless a prompt
  is supplied. Claude, Codex, OpenCode, and Pi require their host config
  directories; Copilot needs a token from pass or `gh` (see
  [GitHub Copilot](#github-copilot)); Bash is always available.
- `--model MODEL_ID` selects the agent model and `--effort {low,medium,high,xhigh,max}`
  selects the reasoning effort. Both apply to interactive and headless runs: the
  CLI forwards them as `PROJECT_SANDBOX_MODEL` / `PROJECT_SANDBOX_EFFORT`, and the
  entrypoint turns them into each agent's own flags — `--model` plus `--effort`
  for Claude, `--model` plus `-c model_reasoning_effort=...` for Codex, `--model`
  plus `--variant` for OpenCode, `--model` plus `--thinking` for Pi, and
  separate `--model` and `--effort` flags for Copilot. Pi model
  values are never rewritten, so `provider/id[:thinking]` forms remain intact,
  and every `--effort` level maps onto a Pi thinking level. They are ignored for
  Bash.
- `--fast` turns on the agent's fast mode for Claude and Codex, in interactive
  and headless runs. The CLI forwards `PROJECT_SANDBOX_FAST=1`, and the
  entrypoint adds `--settings '{"fastMode":true}'` for Claude or
  `-c service_tier="fast"` for Codex. Fast mode bills at a higher rate and only
  works with models and plans that offer it; the agent decides what happens
  otherwise. `--fast` is rejected for OpenCode, Pi, Copilot, and Bash. It combines with
  `--model` and `--effort`, e.g. `--agent claude --model opus --fast`.
- `--pi-tools TOOLS` sets Pi's comma-separated tool allowlist; pass
  `--pi-tools read,grep,find,ls` for a read-only session. Only meaningful with
  `--agent pi`; when omitted, Pi's own default tool set applies, so the agent
  can still edit files and run commands. Pi headless sessions also approve
  project-local resources and write Pi's JSON event stream to the structured
  session log (with a Markdown transcript sidecar). Because Pi's JSON output
  mode always exits 0, project-sandbox inspects that stream and reports exit 1
  when the final turn errored or was aborted.
- `--pi-ollama` (only with `--agent pi`) extends the firewall to reach a
  host-run Ollama server and pre-configures Pi to use it as the default
  provider while Ollama remains bound to `127.0.0.1:11434`. It is a no-op with
  any other `--agent`. The runtime adapter forwards exactly the planned ports
  over pasta (`--network=pasta:-4,-T,<port>:<port>`, with `host.docker.internal`
  mapped to the container's `127.0.0.1`) for Podman on Linux with pasta (the
  default), uses native host-loopback forwarding for other Podman setups
  (slirp4netns, Podman machine) and Docker Desktop, and an
  exact-bridge forwarder for local Linux Docker. That forwarder is part of
  project-sandbox (no `socat` needed) and is shared: concurrent sessions that
  use the same service share one forwarder on `<gateway>:<port>`, which exits
  shortly after the last of them ends. If another program already listens on
  that address and port, startup fails with "Cannot bind … another process
  owns that address and port". VM-backed native aliases
  are accepted only when the container startup probe reaches Ollama. Unsupported modes fail
  closed; project-sandbox never falls back to `0.0.0.0`.

  Apple `container` requires a one-time localhost DNS mapping shared by Ollama
  and agent-proxy forwarding:

  ```bash
  sudo container system dns create host.docker.internal --localhost 203.0.113.113
  ```

  This DNS/PF change can disrupt container Internet access until runtime
  networking is rebuilt and might disable Private Relay. Restart the container
  system after creating the mapping with `container system stop` followed by
  `container system start`. A mapping that resolves but no longer redirects must
  be deleted and re-created; see
  [Apple container host alias](apple-container-dns.md). Combining `--pi-ollama`
  with `--no-firewall` remains
  unsupported because the port-scoped allow rule and connectivity probe live in
  firewall initialization.
- `--ollama-model MODEL_ID` overrides the built-in default Ollama model list
  baked into Pi's `models.json`. Repeatable; only meaningful with
  `--pi-ollama`. The first model (default or first `--ollama-model` given)
  becomes Pi's default model.
- `--log FILE` overrides the default log path,
  `projects/<key>/sessions/<agent>-<branch>-<timestamp>.log` in the project's
  private state. The CLI prints the log path when the run starts and ends, and
  writes a Markdown transcript beside it (`<log stem>.md`).
- `--json-summary PATH` writes one compact JSON object after the session and
  its cleanup complete. Use `-` to print the JSON line on stdout. The
  object contains `session_id`, `container_name`, `workspace_path`, `agent`,
  `bookmark`, `jj_change_id`, `exit_code`, `pid` (of the CLI), `log_path`,
  `started_at`, and `ended_at`.
  `workspace_path` is the session directory in an isolated session and the
  project otherwise. `bookmark` and `jj_change_id` (the result commit) are null
  unless the session is isolated.
  Writing the summary is best-effort: an IO failure prints a warning and leaves
  the session's own exit code intact, so an unusable `PATH` is rejected up front
  instead.
- An isolated session's directory is predictable:
  `<repo>-workspaces/<bookmark>`. A bookmark containing `/` has its slashes
  replaced with `-` and gains a short disambiguating suffix, so `feat/x` becomes
  `feat-x-<6 hex chars>` (the first six hex digits of `sha256("feat/x")`) and
  cannot collide with a literal `feat-x` bookmark. Rather than recomputing the
  name, read the exact absolute path from `workspace_path` in the JSON summary.
  See [Version Control Modes](#version-control-modes).
- Headless Claude, Codex, Pi, OpenCode, and Copilot runs retain their raw JSON event log,
  translate completed messages and tool activity to Markdown on the terminal as
  the session runs, and render the same readable transcript beside the log.
  Transcript generation is best-effort: a parse failure prints a warning but
  never fails the run.
- `--runtime {auto,apple-container,docker,podman}` selects the direct-run
  backend. `auto` prefers Apple `container` on macOS and Docker then Podman on
  Linux.
- `--timeout SECONDS` stops the session if the agent runs too long: the container
  is stopped by name, the CLI process group is also signalled as a fallback, and
  the CLI returns exit code `124`.
- `--verbose` controls how much is shown on the terminal. By default it is quiet:
  image build and Apple `container system start` output is suppressed unless they
  fail, the in-container firewall banner is silenced, interactive runs just print
  `Starting container...` before handing off to the agent/shell, and headless
  runs print the log path up front and a `Wrote N lines to ...` summary at the
  end. With `--verbose`, the build output streams, the firewall banner shows, and
  additional raw non-agent output is teed live to the terminal as well as the
  log. Live agent progress is shown as Markdown in either mode. Verbose mode
  also prints the resolved coding-agent config (agent, model, effort, and fast
  mode for Claude/Codex) before launch. The entrypoint echoes those values and
  the exact agent argv from inside the container; a blank model/effort/fast
  value means the env var did not arrive.
- The agent's exit code is propagated, so CI pipelines can detect failures. Pi's
  JSON output mode always exits 0, so a headless Pi run whose last assistant
  message ended in `error`/`aborted` is reported as exit `1` (or `75` when the
  log identifies a rate limit); an errored turn that a later auto-retry
  recovered from stays a success. Copilot's JSONL schema and failed-run exit
  codes are undocumented, so an exit-0 headless Copilot run is reported as
  exit `1` (or `75` for a rate limit) only when the final record of its stream
  is an error record; anything ambiguous keeps the original exit code.

Unsupervised sessions skip the interactive `-it` flags and switch dispatch to
`<agent>-headless` for all supported agents. Claude runs with
`--dangerously-skip-permissions`, Codex uses `approval_policy = "never"`,
OpenCode runs via `opencode run --format json`, Pi runs with
`-p --mode json --approve` (Pi has no interactive trust prompt to answer
headlessly, so `--approve` is always passed), Copilot runs with
`-p "$PROMPT" --allow-all --no-ask-user --output-format json`, and Bash runs
with `bash -lc`.
The container is still the sandbox boundary; review the diff before
integrating.

A maliciously crafted file in the workspace, such as a prompt-injection in a
README, can still steer an unsupervised agent. Use narrow prompts and inspect the
diff before merging.

## Updating Pinned Dependencies

Pinned package and tool versions are updated with the interactive helper:

```bash
uv run python scripts/update-pins.py
```

By default, a release must have been published for at least 24 hours before
it is considered for adoption. Change that safety window with
`--min-age-hours`, for example `--min-age-hours 48`; use `0` to disable it.
If the newest release is inside the safety window, the script considers the
newest release that is old enough and prints a note about the newer release.
Stable version ordering is used when choosing candidates; alpha, beta, release
candidate, and development versions are ignored, and updates are only offered
when the selected version is newer than the current pin.

The script checks:

- exact PyPI pins in `pyproject.toml`, regenerating `uv.lock` after accepted
  direct dependency updates;
- lockfile-only PyPI package pins in `uv.lock`, updated through
  `uv lock --upgrade-package`;
- global npm tool pins in the generated Dockerfile template;
- pinned Node.js and jj binary releases, including their per-architecture SHA256
  checksums;
- the pinned `ghcr.io/astral-sh/uv` image tag and digest used by the project
  Dockerfile helpers.

For each changed upstream version, the script asks before editing so individual
updates can be accepted or skipped. After accepting updates, run the usual
verification commands:

```bash
uv run python -m compileall src tests scripts
uv run pytest -q
```

For session-time proxy trust, use repeatable [`--ca-cert PATH`](internet-proxy.md#injecting-proxy-ca-certificates) with `--internet-proxy`. The devcontainer has no session-time trust; give it a CA it needs with `--build-ca-cert`.

In an isolated session, `--python-uv` builds the image from the session
directory, including its `pyproject.toml` and `uv.lock`. Generated sandbox
files live in that workspace so the baked environment matches
the sources mounted at `/workspace`. Builds use the runtime's layer cache;
`--no-build` still skips rebuilding, so the existing image must already contain
the bookmark's dependencies.
