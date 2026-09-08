# TODO - outstanding items for next release

## `--python-uv`: stale venv against the mounted workspace

`uv sync --frozen` bakes `/opt/venv` from the build context, but at runtime
`/workspace` is a bind mount whose contents uv has not seen. Two consequences,
both reproducible with `scripts/e2e-python-uv.sh` as a starting point:

- Every `uv run` rebuilds and reinstalls the local project ("Building x @
  file:///workspace", "Uninstalled 1 package"). Harmless but wasteful; it
  happens even when the sources are identical to the build.
- With `--branch`, the image is built from the main project tree
  (`_resolve_build_source` runs before the worktree exists) while the worktree
  is what gets mounted. A branch that changes `pyproject.toml` / `uv.lock`
  therefore needs packages the baked venv lacks, and the entrypoint's
  `UV_OFFLINE=1` turns that into a hard failure:
  "Network connectivity is disabled, but the requested data wasn't found in
  the cache".

Options: build from the worktree once `--branch` has resolved it, or reconcile
with a `uv sync` during the entrypoint's pre-firewall window.
