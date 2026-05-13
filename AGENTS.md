# Repository Guidelines

## Project Structure & Module Organization

Source code lives in `src/project_sandbox/`. The CLI entry point is `cli.py`, runtime command construction is in `container_cli.py`, and generated assets are rendered from `src/project_sandbox/templates/`. Tests live in `tests/` and mirror behavior by module or feature, for example `tests/test_cli.py` and `tests/test_renderers.py`. User-facing usage belongs in `README.md`.

## Build, Test, and Development Commands

Use uv for the local environment:

```bash
uv sync
uv run project-sandbox --help
uv run python -m compileall src tests
uv run pytest -q
```

`uv sync` installs dependencies from `pyproject.toml` / `uv.lock`. The compile command catches syntax errors. `pytest -q` runs the full test suite. For behavior previews, use `uv run project-sandbox --dry-run ...`; dry-run must not write files or start containers.

## Coding Style & Naming Conventions

Use Python 3.11+ syntax, 4-space indentation, explicit type hints where they clarify public helpers, and small modules with focused responsibilities. Keep file and function names lowercase with underscores. Prefer `Path` objects over string path manipulation. Render generated text through templates rather than building large shell or JSON strings inline. Quote shell-facing values with `shlex.quote` or an equivalent template filter.

## Testing Guidelines

Tests use `pytest` with standard `unittest` assertions. Name files `test_*.py` and write tests around externally visible behavior: CLI validation, rendered file contents, command argv construction, and non-mutating dry-runs. Add regression tests for every bug fix. Do not require Apple `container` for unit tests; mock or inspect command construction instead.

## Commit & Pull Request Guidelines

This repository uses Jujutsu. Before editing, create or move onto an empty jj change for your work and give it a clear description, for example:

```bash
jj new -m "Add repository contributor guide"
```

Keep unrelated edits in separate changes. Recent history uses short imperative descriptions such as `Add devcontainer configuration` and `Implement project-sandbox hardening next steps`. Pull requests should describe behavior changes, list verification commands, and call out generated-file or security-impacting changes.

## Security & Configuration Tips

Treat generated container config and firewall behavior as security-sensitive. Avoid passing secrets through environment variables; prefer mounted credential files. The `--branch` flag is active: it mounts the worktree directory at `/workspace` and bind-mounts the main repo's `.git/` at its absolute host path so `git` works inside the container (via VirtioFS UID remapping).
