# project-sandbox

`project-sandbox` initializes a per-project sandbox for Claude Code and Codex CLI using Apple's `container` runtime. It generates a derived image context, sanitized agent configs, launcher scripts, and optional devcontainer output so the same environment can be used from an IDE.

## Supported Path

The current implementation targets macOS with Apple's [`container`](https://github.com/apple/container) CLI installed and running. Use Debian or Ubuntu based images for v0.1, such as `python:3.12-slim`, because the generated Dockerfile installs firewall dependencies with `apt`.

The generated container runs agents as a non-root `agent` user. The container is treated as the primary sandbox boundary, and the generated Claude and Codex configs disable host-oriented approval prompts inside that boundary.

## Install

From a published package:

```bash
uvx project-sandbox --help
```

From a checkout:

```bash
python -m pip install -e .
project-sandbox --help
```

## First Run

Generate the sandbox files, build the image, and start the default agent:

```bash
project-sandbox --agent both /absolute/path/to/repo python:3.12-slim
```

For a planning pass that makes no changes and runs no containers:

```bash
project-sandbox --dry-run --agent both /absolute/path/to/repo python:3.12-slim
```

Generate only the devcontainer files:

```bash
project-sandbox --devcontainer-only /absolute/path/to/repo python:3.12-slim
```

Run an unsupervised session from a prompt file:

```bash
project-sandbox \
  --agent claude \
  --prompt /absolute/path/to/prompt.txt \
  --log /absolute/path/to/session.log \
  /absolute/path/to/repo \
  python:3.12-slim
```

## Generated Files

By default the tool writes:

- `.project-sandbox/Dockerfile`
- `.project-sandbox/entrypoint.sh`
- `.project-sandbox/init-firewall.sh`
- `.project-sandbox/claude/settings.json`
- `.project-sandbox/codex/config.toml`
- `.project-sandbox/bin/run-claude`
- `.project-sandbox/bin/run-codex`
- `.devcontainer/devcontainer.json`
- `.devcontainer` symlinks to selected `.project-sandbox` assets

The tool also appends project-sandbox secret paths to the project `.gitignore`.

## Network Firewall

When the firewall is enabled, launcher scripts add `NET_ADMIN` and `NET_RAW` capabilities so the entrypoint can install an `iptables`/`ipset` egress allowlist before starting the agent. The allowlist includes GitHub, Anthropic, npm, and Codex's OpenAI API endpoint when Codex is installed.

Use `--extra-domain DOMAIN` to allow additional package registries or internal services. Use `--no-firewall` only for trusted debugging scenarios.

## Current Limitations

- `--branch` is temporarily disabled. The first implementation mounted only the worktree directory, but Git worktrees need additional metadata from the main repository to make `git` commands work inside the container. This needs a safer metadata-mount design before re-enabling.
- Base images should be Debian or Ubuntu based.
- Apple `container` is required for launcher execution. Devcontainer output can be used with Docker-compatible devcontainer tooling.
- Credentials are copied from mounted `~/.claude` and `~/.codex` directories into the container. With `--credentials-mode ro`, refresh tokens may not be persisted back to the host.
- Env vars passed to `container run` can be visible in runtime logs. Tokens are passed through mounted files, not env vars.

## Development

This repository includes a devcontainer for local development. Open the checkout in VS Code or Cursor and choose "Reopen in Container".

Create a local uv-managed virtualenv:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . pytest
```

Run static syntax checks:

```bash
.venv/bin/python -m py_compile $(find src tests -name '*.py')
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

The tests cover basic CLI smoke behavior, dry-run non-mutation, renderer output, launcher shell quoting, command construction, jj config rendering, and Python-native unsupervised-session timeout handling.

## Acknowledgements

This project was inspired by [agentbox](https://github.com/fletchgqc/agentbox/tree/main), which pioneered the pattern of running AI coding agents inside disposable container sandboxes.
