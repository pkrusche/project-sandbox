# Development Guide

## Local Setup

Use uv for the local environment:

```bash
uv sync
uv run project-sandbox --help
uv run python -m compileall src tests
uv run pytest -q
```

`uv sync` installs dependencies from `pyproject.toml` / `uv.lock`. The compile
command catches syntax errors. `pytest -q` runs the full test suite. For behavior
previews, use `uv run project-sandbox --dry-run ...`; dry-run must not write
files or start containers.

## Tests

Tests cover CLI surface, runtime selection, dry-run non-mutation, renderer
output, container `argv` construction, devcontainer JSON validity and symlinks,
gitignore helpers, and Python-native unsupervised-session timeout handling.

A self-contained end-to-end smoke test creates a throwaway hello-world project,
runs the tool against it, and validates every generated artefact:

```bash
./scripts/e2e-test.sh                  # portable: devcontainer-only path
./scripts/e2e-test.sh --with-container # also exercises direct CLI container runs
```

The test prints the temp project path on success so the generated files can be
inspected.
