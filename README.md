# project-sandbox

`project-sandbox` initializes a per-project sandbox for Claude Code and Codex CLI using Apple's `container` runtime, with generated config, image context, launcher scripts, and devcontainer output.

## Install

```bash
uvx project-sandbox --help
```

## Usage

```bash
project-sandbox [OPTIONS] PROJECT BASE_IMAGE
```

Example:

```bash
project-sandbox --agent both /absolute/path/to/repo python:3.12-slim
```

## Notes

- Uses absolute mount paths.
- Generates assets under `.project-sandbox/`.
- Optionally writes `.devcontainer/` aligned to the same sandbox image/context.
