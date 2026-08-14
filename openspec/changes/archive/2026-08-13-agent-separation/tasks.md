## 1. Credential Policy

- [x] 1.1 Add a centralized helper that normalizes interactive/headless runtime modes and returns the allowed credential agents, with named agents mapping to themselves and bash modes mapping to all supported agents
- [x] 1.2 Add unit tests covering every named interactive/headless mode, both bash modes, and fail-closed handling of unknown named modes

## 2. Direct Runtime Isolation

- [x] 2.1 Filter the staged credential map in the CLI before constructing direct container or chroot runs while retaining the complete map for devcontainer rendering
- [x] 2.2 Extend shared mount construction to enforce the effective agent's credential allowlist even when callers supply credential directories for every agent
- [x] 2.3 Wire the effective runtime agent through direct-container mount construction without changing provisioning's presence-driven copy behavior, while retaining chroot's bash-only multi-agent mounts
- [x] 2.4 Preserve `--no-forward-credentials` purge and no-mount behavior across named-agent and bash modes

## 3. Credential Mount Regression Coverage

- [x] 3.1 Add direct-run tests for Claude, Codex, OpenCode, and Pi asserting the selected secret mount is present and every unrelated secret mount is absent
- [x] 3.2 Add equivalent headless-mode coverage confirming base-agent credential selection
- [x] 3.3 Add container mount-builder tests proving over-broad credential inputs are filtered for named agents and retained for bash
- [x] 3.4 Add chroot command/mount tests confirming its supported bash mode retains all detected credentials and `--no-forward-credentials` retains none
- [x] 3.5 Confirm devcontainer tests retain and explicitly assert all detected credential mounts (pre-existing coverage; no devcontainer test changes were needed)

## 4. Alternate Reachability Boundaries

- [x] 4.1 Confirm regression tests cover synchronized credentials living only beneath the private staging root and legacy credential files being absent from generated agent config directories (pre-existing coverage in `tests/test_renderers.py`)
- [x] 4.2 Verify workspace `.project-sandbox` and `.devcontainer` masks remain ordered after custom mounts and expose only the empty read-only mask (pre-existing coverage in `tests/test_cli.py`)
- [x] 4.3 Add renderer/build assertions that staged credential paths and credential files are never copied into image layers or generated configuration mounts
- [x] 4.4 Confirm provisioning creates credential files only for secret mounts authorized by the effective execution mode

## 5. Documentation

- [x] 5.1 Document named-agent credential isolation and the intentionally multi-agent credential exposure of `--agent bash` in `docs/security.md`
- [x] 5.2 Document that generated devcontainers receive all detected forwarded agent credentials and point users to `--no-forward-credentials` for an unauthenticated environment
- [x] 5.3 Clarify that private host staging is not a container mount as a whole and that explicit user `--mount` values remain user-authorized exposure

## 6. Verification

- [x] 6.1 Run `uv run python -m compileall src tests`
- [x] 6.2 Run focused credential, container CLI, CLI, devcontainer, and renderer tests
- [x] 6.3 Run `uv run pytest -q`
- [x] 6.4 Inspect dry-run commands for each named agent and bash with all host credential types staged, confirming the expected secret mount set
