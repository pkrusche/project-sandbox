## Why

project-sandbox currently supports three coding agents (Claude, Codex, OpenCode) but not Pi (pi.dev, npm package `@earendil-works/pi-coding-agent`, binary `pi`). Users who prefer Pi's multi-provider BYOK model have no way to run it inside the sandbox's firewalled container. There is no shared agent registry in this codebase — each agent is hardcoded by name across ~10 modules and their tests/docs — so adding Pi means touching each the same way the OpenCode and Codex additions did.

## What Changes

- Add `"pi"` as a fourth supported agent name across the CLI surface (`SUPPORTED_AGENTS`, `_CONFIGURED_AGENTS`, `_agent_host_paths`, `credential_dirs` comprehensions).
- Install the `pi` binary in the container image via a new `install_pi` Dockerfile template flag (npm package `@earendil-works/pi-coding-agent`, pinned to published version `0.80.6`).
- Mount Pi credentials (`~/.pi/agent/auth.json`, mode 0600) read-write into the container at `/project-sandbox-secrets/pi`, following the Codex pattern (`_sync_generic_credentials(..., include_files=("auth.json",))`) since Pi's credential shape is one flat file, not a directory tree like OpenCode's.
- No `/project-sandbox-config/pi` mount and no baked config file — Pi is BYOK by default like OpenCode, so `_warn_opencode_provider_allowlist` is generalized (or an equivalent warning is added) to cover Pi's non-Anthropic/non-OpenAI provider traffic needing `--allow-github`/`--extra-domain`.
- Add `pi`/`pi-headless` case arms to `entrypoint.sh.j2`: interactive `exec pi`, headless `exec pi -p "$PROMPT" --approve` (Pi has no built-in permission system, so `--approve` — which trusts project-local `.pi/` config for one run — must always be passed in headless mode since there is no interactive trust prompt to answer).
- Set telemetry/update-check suppression via env vars in the entrypoint/provision scripts (`PI_SKIP_VERSION_CHECK=1`; PI_OFFLINE=1 / use --offline for suppression of telemetry.
- Support Pi's combined `--model`/`--effort` flag shape (`--model sonnet:high`, one flag not two) in CLI model/effort injection for headless runs.
- Extend `devcontainer.py`'s credentials-only mount pattern (mirroring OpenCode) and `_credential_dirs()` to include Pi.
- Update docs (`README.md`, `docs/usage.md`, `docs/runtime.md`, `docs/security.md`) to list Pi as a supported agent.
- Explicitly out of scope for this change: `oauth_refresh.py` delegation and `token_expiry.py` OAuth-expiry parsing for Pi. Both modules are defensive by construction (unknown agent → silent no-op / `None`, never raises), so it is safe to ship without wiring them and fill in once Pi's `auth.json` OAuth-entry shape and any `pi login status`-equivalent subcommand are confirmed against Pi's actual source — same posture as OpenCode today (no delegated refresh).
- No transcript.py renderer for Pi — Pi's headless output is plain text, not structured JSON, matching the OpenCode precedent of no markdown transcript.

## Capabilities

### New Capabilities
- `pi-agent-support`: end-to-end support for running the Pi coding agent inside the sandboxed container — image build, credential mounting, headless/interactive dispatch, model/effort flag translation, and provider-allowlist warnings.

### Modified Capabilities
(none — no existing `openspec/specs/` capabilities exist yet to modify; this is the first capability spec written for this repo)

## Impact

- **Code**: `cli.py`, `container_cli.py`, `config_agents.py`, `dockerfile.py` + `templates/Dockerfile.j2`, `devcontainer.py`, `templates/entrypoint.sh.j2` + `templates/_provision.sh.j2`.
- **Explicitly not touched**: `oauth_refresh.py`, `token_expiry.py` (deferred, see above).
- **Tests**: `tests/test_cli.py`, `tests/test_container_cli.py`, `tests/test_renderers.py`, `tests/test_devcontainer.py` — mirror existing Codex/OpenCode coverage (headless dispatch, model/effort injection, credential mount, Dockerfile install-line assertions).
- **Docs**: `README.md`, `docs/usage.md`, `docs/runtime.md`, `docs/security.md`.
- **Dependencies**: adds `@earendil-works/pi-coding-agent@0.80.6` to the container image's npm install set.
- **Security-sensitive**: new credential mount path (`/project-sandbox-secrets/pi`) and firewall-allowlist warning behavior — both fall under this repo's "treat generated container config and firewall behavior as security-sensitive" guidance.
