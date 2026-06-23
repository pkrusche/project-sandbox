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

In this mode, `.project-sandbox/Dockerfile` starts with the existing Dockerfile
contents and appends the sandbox runtime, firewall, OpenSpec, and installed
coding agents. The build context defaults to the project root so existing `COPY`
instructions keep working; use `--docker-context` if that Dockerfile expects a
different context. If the source Dockerfile defines its own non-root user or
standalone UID/GID setup, project-sandbox removes those instructions, prints a
warning, and creates its own `agent` user with UID 1000. If a `RUN` instruction
mixes user-management commands with unrelated build steps, project-sandbox
rejects it instead of silently dropping the unrelated work.

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

COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 /uv /usr/local/bin/uv

# Pre-populate the uv package cache with all project dependencies so the agent
# can run `uv sync` / `uv run` inside the sandbox without reaching PyPI.
# UID 1000 is the agent user created by the sandbox layers that follow.
COPY pyproject.toml uv.lock README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R 1000:1000 /opt/uv-cache \
    && rm -rf /tmp/project-setup

ENV UV_CACHE_DIR=/opt/uv-cache
```

Key points:

- Copy all files referenced by `pyproject.toml` metadata, including `README.md`
  if declared as `readme =`, so the build backend can validate the project
  during the cache-warming step.
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
- `--agent {claude,codex,opencode,bash}` selects which agent to run. If omitted,
  the CLI only initializes generated config files unless a prompt is supplied.
  Claude, Codex, and OpenCode require their host config directories; Bash is
  always available.
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
  headless output is teed live to the terminal as well as the log.
- The agent's exit code is propagated, so CI pipelines can detect failures.

Unsupervised sessions skip the interactive `-it` flags and switch dispatch to
`<agent>-headless` for all supported agents. Claude runs with
`--dangerously-skip-permissions`, Codex uses `approval_policy = "never"`,
OpenCode runs via `opencode run`, and Bash runs with `bash -lc`. The container is
still the sandbox boundary; review the diff before integrating.

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
