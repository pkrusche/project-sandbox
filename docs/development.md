# Development Guide

## Local Setup

Use uv for the local environment:

```bash
uv sync
uv run project-sandbox --help
uv run python -m compileall src tests
uv run pytest -q
./scripts/check-ruff.sh
```

`uv sync` installs dependencies from `pyproject.toml` / `uv.lock`. The compile
command catches syntax errors. `pytest -q` runs the full test suite.
`scripts/check-ruff.sh` verifies that Python files have Ruff formatting applied
and contain no Ruff lint violations. For behavior previews, use
`uv run project-sandbox --dry-run ...`; dry-run must not write files or start
containers.

## Image build cache

`build_cache.py` fingerprints the generated build inputs and records the
fingerprint plus image tag in `.project-sandbox/.build-state.json`. `cli.py`
skips the build when the fingerprint matches and `container_cli.image_exists()`
confirms the image is present; auto-skip is limited to the default flow where the
build context equals the generated `.project-sandbox` dir. `dockerfile.render_dockerignore()` writes a scoped `Dockerfile.dockerignore`
(BuildKit's per-Dockerfile ignore convention) for the `--python-uv` flow only —
the one whole-project context whose Dockerfile we generate — so it doesn't tar
virtualenvs/caches to the daemon. It is skipped for user-supplied `--dockerfile`
builds (which may copy those paths) and when the project has its own root
`.dockerignore` (left authoritative). All of this degrades safely: any mismatch
or inconclusive check falls through to a normal build.

## Tests

Tests cover CLI surface, runtime selection, dry-run non-mutation, renderer
output, container `argv` construction, devcontainer JSON validity and symlinks,
gitignore helpers, image-build caching, and Python-native unsupervised-session
timeout handling.

A self-contained end-to-end smoke test creates a throwaway hello-world project,
runs the tool against it, and validates every generated artefact:

```bash
./scripts/e2e-test.sh                  # portable: devcontainer-only path
./scripts/e2e-test.sh --with-container # also exercises direct CLI container runs
```

The test prints the temp project path on success so the generated files can be
inspected.

Branch workflow end-to-end tests exercise real headless bash-agent runs against
throwaway git and jj repositories. They verify the finish actions that integrate
or leave agent work after the session:

```bash
./scripts/e2e-env-injection.sh
./scripts/e2e-git-workflow.sh
./scripts/e2e-jj-workflow.sh
```

All three scripts default to `--runtime chroot` on Linux and accept
`--runtime chroot|auto|apple-container|docker|podman`, `--base-image IMAGE`,
`--no-build`, and `--keep`. Run `./scripts/run-e2e-tests.sh` to execute the
smoke, env-injection, git, and jj suites together with the same defaults.
