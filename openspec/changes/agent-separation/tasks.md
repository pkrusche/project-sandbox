## 1. Credential Policy

- [ ] 1.1 Add a centralized helper that normalizes interactive/headless runtime modes and returns the allowed credential agents, with named agents mapping to themselves and bash modes mapping to all supported agents
- [ ] 1.2 Add unit tests covering every named interactive/headless mode, both bash modes, and fail-closed handling of unknown named modes

## 2. Direct Runtime Isolation

- [ ] 2.1 Filter the staged credential map in the CLI before constructing direct container or chroot runs while retaining the complete map for devcontainer rendering
- [ ] 2.2 Extend shared mount construction to enforce the effective agent's credential allowlist even when callers supply credential directories for every agent
- [ ] 2.3 Wire the effective runtime agent through direct-container mount construction without changing provisioning's presence-driven copy behavior, while retaining chroot's bash-only multi-agent mounts
- [ ] 2.4 Preserve `--no-forward-credentials` purge and no-mount behavior across named-agent and bash modes

## 3. Credential Mount Regression Coverage

- [ ] 3.1 Add direct-run tests for Claude, Codex, OpenCode, and Pi asserting the selected secret mount is present and every unrelated secret mount is absent
- [ ] 3.2 Add equivalent headless-mode coverage confirming base-agent credential selection
- [ ] 3.3 Add container mount-builder tests proving over-broad credential inputs are filtered for named agents and retained for bash
- [ ] 3.4 Add chroot command/mount tests confirming its supported bash mode retains all detected credentials and `--no-forward-credentials` retains none
- [ ] 3.5 Update devcontainer tests to retain and explicitly assert all detected credential mounts

## 4. Alternate Reachability Boundaries

- [ ] 4.1 Add regression tests confirming synchronized credentials live only beneath the private staging root and legacy credential files are absent from generated agent config directories
- [ ] 4.2 Verify workspace `.project-sandbox` and `.devcontainer` masks remain ordered after custom mounts and expose only the empty read-only mask
- [ ] 4.3 Add renderer/build assertions that staged credential paths and credential files are never copied into image layers or generated configuration mounts
- [ ] 4.4 Confirm provisioning creates credential files only for secret mounts authorized by the effective execution mode

## 5. Documentation

- [ ] 5.1 Document named-agent credential isolation and the intentionally multi-agent credential exposure of `--agent bash` in `docs/security.md`
- [ ] 5.2 Document that generated devcontainers receive all detected forwarded agent credentials and point users to `--no-forward-credentials` for an unauthenticated environment
- [ ] 5.3 Clarify that private host staging is not a container mount as a whole and that explicit user `--mount` values remain user-authorized exposure

## 6. Verification

- [ ] 6.1 Run `uv run python -m compileall src tests`
- [ ] 6.2 Run focused credential, container CLI, CLI, devcontainer, and renderer tests
- [ ] 6.3 Run `uv run pytest -q`
- [ ] 6.4 Inspect dry-run commands for each named agent and bash with all host credential types staged, confirming the expected secret mount set
