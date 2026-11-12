# Usage guide

## Install

```bash
uv tool install project-sandbox      # or run without installing: uvx project-sandbox --help
project-sandbox doctor               # check runtimes, git and jj
```

From a source checkout: `uv sync`, then `uv run project-sandbox …`.

`project-sandbox doctor` is read-only (each probe times out after 10 s) and
exits `1` only when a check fails; `--json` prints the same report for scripts.
It checks Python ≥ 3.11; Apple `container` (version, system service and the
[`host.docker.internal` alias](apple-container-dns.md)); Docker (client and
daemon); Podman (must be rootless); the chroot runtime on Linux; which runtime
`--runtime auto` would pick and whether it works; `git`, its global identity,
and `jj` ≥ 0.46; and, per runtime, whether the [egress sidecar](egress-sidecar.md)
runs.

## Quick start

```bash
project-sandbox /path/to/repo python:3.12-slim                 # generate files only
project-sandbox --dry-run --agent claude /path/to/repo python:3.12-slim
project-sandbox --agent claude /path/to/repo python:3.12-slim  # build and start a session
```

`--dry-run` previews every action without writing files or starting a runtime.
Agents: `claude`, `codex`, `opencode`, `pi`, `copilot` and `bash`. Claude,
Codex, OpenCode and Pi need their host config directories; Copilot needs a
token (see [GitHub Copilot](#github-copilot)); Bash is always available.

**Runtimes.** `--runtime auto` (default) picks Apple `container` on macOS and
Docker, then Podman, on Linux; pass `--runtime docker|podman|apple-container`
to choose. Podman must be rootless and runs with `--userns=keep-id`; on Linux
with pasta it gives builds and sessions their own address (`198.18.0.2/24`) so
services on the host's LAN address stay reachable. `--runtime chroot` (Linux)
only inspects the mount layout; it is not a sandbox (see
[runtime](runtime.md#end-to-end-flow)).

**Project paths** must not contain control characters, `*`, `?`, `[`, quotes,
commas, colons or backslashes; rename such directories.

**Resources.** `--memory`, `--cpus` and `--shm-size` (default `2g` with
`--node-npm`) are passed to the runtime.

**Extra mounts.** `--mount` takes Docker's `--mount` syntax with CSV quoting
(quote a whole field to include a comma, e.g.
`'type=bind,"source=/host/cache,shared",target=/cache'`). Only bind mounts with
a checkable host source are accepted; `bind-propagation`, `bind-recursive`,
`bind-nonrecursive` and `consistency` are passed through. Mounts that would
expose project-sandbox's own state or the egress policy are refused.

## Choosing the image

Give either a base image, a generated flow, or your own Dockerfile.

| Option | Base | Notes |
| --- | --- | --- |
| `IMAGE` (e.g. `python:3.12-slim`) | That image | Debian or Ubuntu based |
| `--python-uv [--python 3.12]` | `python:<version>-slim` | Dependencies from `uv.lock` baked in |
| `--rust-cargo [--rust 1.87]` | `rust:<version>-slim` | Dependencies fetched, best-effort build |
| `--node-npm` | `debian:trixie-slim` | npm dependencies and Chromium baked in |
| `--dockerfile PATH [--docker-context DIR]` | Your Dockerfile's final stage | Sandbox layers added on top |

The generated flows use the whole project as build context (with a generated
`.dockerignore` unless the project has one) and set up the environment so the
agent works offline: uv's cache, environment (`/opt/venv`) and interpreters live
in the image; Cargo dependencies are fetched; npm dependencies and browsers are
installed. With `--branch`, they build from the session's worktree or
workspace.

### Python with uv

`--python-uv` installs the project's locked dependencies in two layers
(dependencies only, then the project) and sets `UV_CACHE_DIR`,
`UV_PROJECT_ENVIRONMENT=/opt/venv` and `UV_PYTHON_INSTALL_DIR`, so `uv sync` and
`uv run` work without PyPI. A custom `--dockerfile` must set
`UV_PROJECT_ENVIRONMENT` itself: otherwise sandbox and host uv keep replacing
the shared `.venv` in the mounted project (symptom: "Ignoring existing virtual
environment linked to non-existent Python interpreter"). Generated Codex configs
pass the uv variables through to tool shells.

### Rust with Cargo

`--rust-cargo` copies workspace member manifests before sources to keep the
dependency cache. Member paths are used literally (spaces, quotes and other
shell characters are safe); paths that escape the project, absolute paths,
control characters and backslashes are rejected.

### Node.js with npm

`--node-npm` needs `package.json` and a committed `package-lock.json` or
`npm-shrinkwrap.json` (npm only; workspaces supported through the lockfile;
pnpm, yarn and bun are not).

- `npm ci` runs at build time from the manifests and lockfile only, into
  `/opt/node-project/node_modules`; a root lifecycle script that needs sources
  fails the build. `.npmrc` is not copied, so private registries and options
  such as `legacy-peer-deps` need a custom Dockerfile.
- Debian Chromium and fonts are always installed; when the lockfile has
  Playwright, its matching Chromium goes to `/opt/ms-playwright`. `CHROME_BIN`,
  `PUPPETEER_EXECUTABLE_PATH` and `PLAYWRIGHT_BROWSERS_PATH` are set. Node.js is
  the sandbox's pinned version.
- At runtime `/workspace/node_modules` is a tmpfs filled from the image: host
  binaries are hidden, writes never reach the host, and every session starts
  fresh. It counts against `--memory` (default `8g`), as does `/dev/shm`.
- Chromium needs `--no-sandbox` in the container (Playwright's default).
- Generated devcontainers don't support `--node-npm` yet.

See [Headless browser testing](headless-browser-testing.md) for a Playwright
setup and [Network access](networking.md#tests-that-need-external-hosts) for
tests that need external hosts.

### Your own Dockerfile

With `--dockerfile`, project-sandbox adds a dependency stage (apt tooling,
Node.js, jj, OpenSpec, agents) that your final stage inherits, then appends the
sandbox user, firewall and entrypoint. The build context is the project root
unless `--docker-context` says otherwise.

- Put prerequisites such as mirrors or CA files in a stage named `prefix` that
  the final stage inherits from; the dependency stage is inserted right after
  it. Use stage names, not numeric `--from=<N>`, for stages after `prefix`.
- Your instructions run as root, before the `agent` user exists. Your own user
  setup is removed with a warning (a `RUN` mixing it with other work is
  rejected); the sandbox creates `agent` with your host UID/GID on Linux Docker
  and Podman, 1000 elsewhere.
- Only the default `\` line continuation is supported, and the final stage must
  be Debian or Ubuntu based.
- The Dockerfile's hash is recorded after each build, and a later change prints
  a warning ([tamper detection](runtime.md#project-dockerfile-tamper-detection)).

For build-time proxies and CAs, see [Proxies for image builds](networking.md#proxies-for-image-builds).

### Image builds

Builds are skipped when the generated inputs and image are unchanged
(`Reusing cached image (inputs unchanged)`); whole-project flows always build
and rely on the layer cache. `--force-build` rebuilds, `--no-build` uses the
existing image as it is (it can't change a baked firewall), and `--build-only`
builds without starting a session (add `--pi-ollama` to prepare a later Pi +
Ollama session). See [Concurrent sessions](runtime.md#concurrent-sessions) for
locking and image pinning.

## Agents and credentials

Named agents get only their own forwarded credentials, staged privately per
session; `--agent bash` gets every detected credential except Copilot's token.
Before staging, the host refreshes Claude and Codex tokens with their own CLIs
(`--no-token-refresh` skips this), and the launch prints how long the staged
token stays valid. See [Security](security.md#credential-isolation).

- `--model MODEL` and `--effort {low,medium,high,xhigh,max}` become each agent's
  own flags (Claude `--effort`, Codex `model_reasoning_effort`, OpenCode
  `--variant`, Pi `--thinking`, Copilot `--effort`); ignored for Bash.
- `--fast` enables fast mode for Claude and Codex (billed at a higher rate);
  rejected for other agents.
- `--no-forward-credentials` starts unauthenticated. With it,
  `--api-key-env NAME` and `--api-key-env-file FILE` inject specific keys as
  environment variables (redacted in dry-run; visible in runtime metadata).
- `--agent-proxy URL` routes model traffic through a gateway; see
  [Agent proxy](agent-proxy.md).
- Pi: `--pi-tools read,grep,find,ls` limits its tools; `--pi-ollama` uses a
  host Ollama server on `127.0.0.1:11434` (forwarding per runtime is described
  in [runtime](runtime.md#end-to-end-flow); Apple needs the
  [alias](apple-container-dns.md)); `--ollama-model` (repeatable) replaces the
  default model list. OpenCode and Pi reach only the default providers unless
  you add `--extra-domain`.

### GitHub Copilot

`--agent copilot` runs the pinned GitHub Copilot CLI on your Copilot
subscription (no `--agent-proxy`, no `--fast`, not in devcontainers).

- **Host.** `--copilot-host` is `github.com` (default; personal, Business and
  Enterprise on GitHub.com) or `<tenant>.ghe.com` (data residency). GitHub
  Enterprise Server is rejected.
- **Token**, resolved on the host at every launch: `pass show
  copilot-github-token` (or `copilot-github-token/<host>`;
  `--copilot-token-pass ENTRY` to rename), then `gh auth token --hostname
  <host>` with a warning, because that token usually has broad `repo` scope.
  Recommended: a fine-grained token with only the **Copilot Requests**
  permission, stored with `pass insert copilot-github-token`. Classic `ghp_`
  tokens are rejected.
- **In the sandbox** the token is a read-only file exported as
  `COPILOT_GITHUB_TOKEN` to the Copilot process only. Copilot runs with
  `--allow-all --no-auto-update --disable-builtin-mcps`; the folder-trust
  prompt is pre-answered. `--agent copilot` opens GitHub network access in
  firewall mode; with `--egress sidecar` it needs the Copilot profile.

## Headless sessions

```bash
project-sandbox --agent claude --prompt /path/to/prompt.txt /path/to/repo python:3.12-slim
project-sandbox --agent codex --prompt-text "Fix the failing test" /path/to/repo python:3.12-slim
```

A prompt makes the session unsupervised: no TTY, agents run without permission
prompts (Claude `--dangerously-skip-permissions`, Codex `approval_policy =
"never"`, Pi `--approve`), output goes to a log, and no shell or Claude history
is mounted.

- `--log FILE` overrides the default
  `.project-sandbox/sessions/<agent>-main-<timestamp>.log`. Claude, Codex, Pi,
  OpenCode and Copilot keep the raw JSON log and write a Markdown transcript
  beside it.
- `--json-summary PATH` (or `-` for stdout) writes `session_id`,
  `container_name`, `workspace_path`, `agent`, `bookmark`, `jj_change_id`,
  `exit_code`, `started_at` and `ended_at` after cleanup.
- `--timeout SECONDS` stops the session and returns `124`. A session that ends
  on HTTP 429 returns `75`. The agent's exit code is passed through otherwise;
  Pi and Copilot runs that exit `0` with an errored final turn report `1`.
- `--verbose` streams build output, the firewall banner and raw output, and
  prints the resolved agent configuration.
- `project-sandbox sessions list --json` lists session records
  (`running`, `completed`, `rate_limited`, `interrupted`, `orphaned`) from
  `$XDG_STATE_HOME/project-sandbox/sessions/`.

A prompt injection in the workspace can still steer an unsupervised agent; keep
prompts narrow and review the diff.

## Branch sessions

`--branch NAME` runs the agent in a Git worktree (or a jj workspace when the
project has `.jj/`) on `NAME`, instead of your working tree:

```bash
project-sandbox --branch feature/login --agent claude --prompt /path/to/prompt.txt /path/to/repo python:3.12-slim
```

At the end, uncommitted changes are committed onto the branch (Git), or the
working copy is snapshotted and the bookmark moved to it (jj). Nothing is merged
or pushed; integrate the branch yourself. The worktree is then removed unless
the session failed or `--keep-workspace` was given, in which case a later run
for the same branch reuses it.

- `--branch-start-at REV` sets the start of a **new** branch (default `HEAD` /
  `@`); it is an error for an existing branch.
- `--worktree-dir DIR` changes the default `<repo>-worktrees/` (Git) or
  `<repo>-workspaces/` (jj); names with `/` become `<name with ->-<6 hex>`.
  The JSON summary's `workspace_path` gives the exact path.
- Concurrent sessions need distinct branches; the same branch is refused while
  in use. jj workspaces are always non-colocated, so the agent uses `jj` there.
- `--vcs-access read-only|hidden` protects the repository metadata from the
  session; see [VCS access](vcs-access.md).

## Devcontainers

Running `project-sandbox /path/to/repo IMAGE` also writes `.devcontainer/`. Run
it on the host first (it stages the credentials the devcontainer mounts), then
choose **Reopen in Container** in VS Code, Cursor or another devcontainer-aware
IDE. The devcontainer builds the same image, mounts the same configs and
credentials, and runs the firewall from `postStartCommand`.

- Use **Rebuild Container** rather than restarting a stopped one: the firewall
  runs once per container and drops its sudo rule afterwards.
- Re-run the `project-sandbox` command when credentials may have changed or
  `/tmp` was cleaned.
- Remote services such as Codespaces need adaptation: the configuration uses
  local paths, `/tmp` credential staging and `NET_ADMIN`/`NET_RAW`.
