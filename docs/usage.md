# Usage Guide

## Install

Directly from GitHub:

```bash
uvx --from git+https://github.com/pkrusche/project-sandbox.git project-sandbox --help
```

From a checkout:

```bash
uv sync
uv run project-sandbox --help
```

## Quick Start

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

To build on top of a repo's existing Dockerfile instead of a base image tag:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

In this mode, `.project-sandbox/Dockerfile` creates a dependency stage for the
sandbox's apt tooling, Node.js, jj, OpenSpec, and coding agents. The source
Dockerfile's final stage inherits that dependency stage before its original
instructions are appended. This keeps the expensive, source-independent layers
cacheable when project files change and preserves earlier build stages and
`COPY --from` references.

If certificates, proxies, or package mirrors must be configured before those
sandbox-owned network installs, put that setup in a stage named `prefix` and
make the final stage inherit from it (directly or through other named stages):

```dockerfile
FROM python:3.12-slim AS prefix
COPY corporate-ca.crt /usr/local/share/ca-certificates/
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
# Match ownership to the agent user created by the sandbox layers that follow.
COPY README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R "${AGENT_UID}:${AGENT_GID}" /opt/uv-cache \
    && rm -rf /tmp/project-setup

ENV UV_CACHE_DIR=/opt/uv-cache
```

Key points:

- The first layer only copies `pyproject.toml` and `uv.lock`, and installs
  dependencies with `--no-install-project` so it doesn't need the project's
  source at all. It only rebuilds when the manifest/lockfile change.
- The second layer copies `README.md` and `src/`, then runs `uv sync` again
  (without `--no-install-project`) to build and install the local project
  itself. Copy all files referenced by `pyproject.toml` metadata, including
  `README.md` if declared as `readme =`, so the build backend can validate the
  project. This layer rebuilds on every source edit, but reuses the dependency
  cache the first layer already populated.
- The `chown -R 1000:1000` gives the sandbox's `agent` user, created by the
  layers that follow, full access to the pre-warmed cache.
- `ENV UV_CACHE_DIR=/opt/uv-cache` persists into the running container so the
  agent's `uv sync` reads from the cache instead of PyPI.

Then run:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

## Devcontainer Flow

Before starting the devcontainer, run
`project-sandbox /absolute/path/to/repo python:3.12-slim` once on the host. This
refreshes the `/tmp/project-sandbox-<uid>/...` agent credential staging
directories that the devcontainer mounts at startup.

Then open the project in VS Code, Cursor, or any devcontainer-aware IDE and
choose **Reopen in Container**. The generated `devcontainer.json` builds the same
image, mounts the same sanitized configs and staged credentials, runs the same
firewall via `postStartCommand`, and waits for it before opening a terminal.
Re-run the same `project-sandbox` command before starting or rebuilding the
devcontainer again whenever credentials may have changed, `/tmp` may have been
cleaned, or you are on a different host.

Generate the devcontainer without building or running anything:

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

This is useful for repos whose owners do not want to run a direct CLI runtime but
want the sandboxed agent environment in a local devcontainer-capable IDE. Remote
devcontainer services such as Codespaces need additional adaptation because the
generated spec uses local `.project-sandbox/` files, absolute host credential
staging under `/tmp/project-sandbox-<uid>/...`, and firewall capabilities
(`NET_ADMIN`/`NET_RAW`) that may not be available remotely.

## Image Builds

The container image is built before a direct-run session starts. To keep
repeated local runs fast, the build is **skipped when nothing changed**:
project-sandbox fingerprints the generated build inputs (the rendered
`Dockerfile`, `entrypoint.sh`, `init-firewall.sh`, the devcontainer init script,
the base image, and the image tag) and records that fingerprint in
`.project-sandbox/.build-state.json`. On the next run, if the fingerprint still
matches and the runtime confirms the image exists, the build is skipped and
`Reusing cached image (inputs unchanged)` is printed; otherwise the image is
rebuilt and the build duration is reported (`Built image in 12.3s`).

- Auto-skip applies to the default base-image flow. `--python-uv` and
  `--dockerfile` builds use the whole project as the build context, so they
  always invoke the build and rely on the runtime's layer cache instead.
- For `--python-uv` only, a generated `.project-sandbox/Dockerfile.dockerignore`
  trims virtualenvs, `node_modules`, and tool caches from the context (it does
  not exclude `.git` — git version backends read it during the in-image install
  — nor `.project-sandbox/`, whose scripts are copied into the image). It is not
  generated for user-supplied `--dockerfile` builds, which may legitimately copy
  those paths. If the project already has a root `.dockerignore`, it is left
  authoritative and no per-Dockerfile file is generated.
- `--force-build` rebuilds even when the cache is valid.
- `--no-build` skips the build entirely, assuming the image already exists.

## Branch Mode

`--branch <name>` runs the agent in an isolated git worktree (or jj workspace, if
the project root has a `.jj/` directory) checked out on `<name>`, instead of on
your working tree. The worktree/workspace is mounted at `/workspace` in the
sandbox and the repo's VCS metadata is bind-mounted so `git`/`jj` work inside.

```bash
uv run project-sandbox \
  --branch feature/login \
  --agent claude --prompt /absolute/path/to/prompt.txt \
  /absolute/path/to/repo python:3.12-slim
```

After the session there is one action, and it never touches your main checkout:

- The agent's work is captured on the branch/bookmark. For git, any uncommitted
  changes are committed onto `<name>`. For jj, the working copy is snapshotted
  and the bookmark is advanced to the session tip (`@`, described if it has no
  message; or `@-` when `@` is empty, so committed work is captured without
  leaving an empty commit as the tip).
- The worktree/workspace is then removed. The branch/bookmark keeps the commits
  for you to merge, rebase, or open a PR from manually.

Related flags:

- `--branch-start-at <revision>` — starting commit/tag/branch/bookmark for a
  **new** branch. Errors if the branch/bookmark already exists (delete or merge it
  first, or omit the flag to reuse it). Without it, a new branch starts at `HEAD`
  (git) / `@` (jj), and an existing branch/bookmark is reused.
- `--keep-workspace` — leave the worktree/workspace in place after the session so
  a later `--branch <name>` run reuses it (the reused workspace resumes from the
  branch/bookmark tip). A failed session is always left in place for inspection.
- `--worktree-dir <path>` — where to place the worktree/workspace root (default:
  a sibling `<repo>-worktrees` / `<repo>-workspaces` directory).

## Unsupervised Sessions

Run the agent without a TTY, starting from a prompt and writing all output to a
log file:

```bash
uv run project-sandbox \
  --prompt /absolute/path/to/prompt.txt \
  /absolute/path/to/repo \
  python:3.12-slim
```

- `--prompt FILE` copies the file into a private generated staging directory,
  bind-mounts only that directory read-only, and reads the prompt from
  `/project-sandbox-prompt/<name>`.
- `--prompt-text "..."` writes the prompt under `.project-sandbox/prompts/`,
  bind-mounts that directory read-only, and reads it from
  `/project-sandbox-prompt/prompt.txt`.
- `--agent {claude,codex,opencode,pi,bash}` selects which agent to run. If
  omitted, the CLI only initializes generated config files unless a prompt is
  supplied. Claude, Codex, OpenCode, and Pi require their host config
  directories; Bash is always available.
- `--model MODEL_ID` selects the agent model and `--effort {low,medium,high,xhigh,max}`
  selects the reasoning effort. Both apply to interactive and headless runs: the
  CLI forwards them as `PROJECT_SANDBOX_MODEL` / `PROJECT_SANDBOX_EFFORT`, and the
  entrypoint turns them into each agent's own flags — `--model` plus `--effort`
  for Claude, `--model` plus `-c model_reasoning_effort=...` for Codex, `--model`
  plus `--variant` for OpenCode, and a single combined `--model <model>:<effort>`
  for Pi (Pi has no separate effort flag). They are ignored for Bash.
- `--pi-ollama` (only with `--agent pi`) extends the firewall to reach a
  host-run Ollama server and pre-configures Pi to use it as the default
  provider while Ollama remains bound to `127.0.0.1:11434`. It is a no-op with
  any other `--agent`. The runtime adapter uses native host-loopback forwarding
  for rootless Podman and Docker Desktop, and an exact-bridge `socat` proxy for
  local Linux Docker/rootful Podman. VM-backed native aliases are accepted only
  when the container startup probe reaches Ollama. Unsupported modes fail
  closed; project-sandbox never falls back to `0.0.0.0`.

  Apple `container` requires one-time, user-controlled localhost DNS setup:

  ```bash
  sudo container system dns create ollama.project-sandbox.internal \
    --localhost 203.0.113.113
  ```

  Run that command yourself before `--pi-ollama`. Project-sandbox verifies the
  mapping but never invokes `sudo` or changes it. Apple documents that localhost
  DNS changes packet-filter state, may disable Private Relay, and may need
  re-establishing after a restart. Combining `--pi-ollama` with `--no-firewall`
  remains unsupported because fixed-hostname setup and the port rule live in
  firewall initialization.
- `--ollama-model MODEL_ID` overrides the built-in default Ollama model list
  baked into Pi's `models.json`. Repeatable; only meaningful with
  `--pi-ollama`. The first model (default or first `--ollama-model` given)
  becomes Pi's default model.
- `--log FILE` overrides the default log path under
  `.project-sandbox/sessions/<agent>-main-<timestamp>.log`.
- For headless `claude` runs, a readable markdown transcript is rendered
  automatically beside the log by parsing the stream-json events. This is
  best-effort: a parse failure prints a warning but never fails the run.
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
  headless output is teed live to the terminal as well as the log. It also prints
  the resolved coding-agent config (agent, model, effort) before launch, and the
  entrypoint echoes the same values plus the exact agent argv from inside the
  container — a blank model/effort there means the env var did not arrive.
- The agent's exit code is propagated, so CI pipelines can detect failures.

Unsupervised sessions skip the interactive `-it` flags and switch dispatch to
`<agent>-headless` for all supported agents. Claude runs with
`--dangerously-skip-permissions`, Codex uses `approval_policy = "never"`,
OpenCode runs via `opencode run`, Pi runs with `--approve` (Pi has no
interactive trust prompt to answer headlessly, so `--approve` is always
passed), and Bash runs with `bash -lc`. The container is still the sandbox
boundary; review the diff before integrating.

A maliciously crafted file in the workspace, such as a prompt-injection in a
README, can still steer an unsupervised agent. Use narrow prompts and inspect the
diff before merging.

## Updating Pinned Dependencies

Pinned package and tool versions are updated with the interactive helper:

```bash
uv run python scripts/update-pins.py
```

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
