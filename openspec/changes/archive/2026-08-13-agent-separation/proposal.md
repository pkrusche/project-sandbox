## Why

Named-agent runs currently stage and mount credentials for every detected coding agent, allowing a Claude, Codex, OpenCode, or Pi process to read unrelated provider credentials. Credential forwarding should follow least privilege while preserving the intentionally multi-agent nature of bash sessions and generated devcontainers.

## What Changes

- Restrict direct named-agent runs to the selected agent's credential mount and in-container credential files.
- Keep `--agent bash` and generated devcontainers multi-agent, and explicitly document that they receive all detected forwarded credentials.
- Define credential-mount selection centrally and enforce it again at runtime mount construction so over-broad staged inputs cannot create unintended mounts.
- Preserve credential-free behavior for `--no-forward-credentials`.
- Add regression coverage that unrelated credentials are not reachable through secret mounts, generated configuration mounts, the workspace staging directory, or the built image.
- Clarify that explicit user-provided bind mounts remain user-authorized and can expose arbitrary host paths.

## Capabilities

### New Capabilities

- `agent-credential-forwarding`: Defines credential forwarding, isolation, multi-agent modes, staging boundaries, and non-secret generated configuration behavior.

### Modified Capabilities

- `pi-agent-support`: Align Pi credential forwarding with the selected-agent isolation policy while retaining Pi credentials in intentionally multi-agent modes.

## Impact

- Credential staging and selection in `config_agents.py` and `cli.py`.
- Direct container and chroot mount construction in `container_cli.py`.
- Devcontainer credential mounts and user-facing security/runtime documentation.
- Provisioning and renderer tests that currently assume all credentials are mounted together.
- No CLI flag or external dependency changes are expected.
