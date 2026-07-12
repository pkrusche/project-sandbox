## 1. Confirmed unknowns

- 1.1 `PI_OFFLINE`'s disable value is PI_OFFLINE=1 for offline mode, equivalent to --offline on the cli
- 1.2 The current published npm version tag for `@earendil-works/pi-coding-agent` matches `0.80.6`, pin to that

## 2. Agent registration

- [ ] 2.1 Add `"pi"` to `SUPPORTED_AGENTS` in `cli.py`
- [ ] 2.2 Add `"pi"` to `_CONFIGURED_AGENTS` in `config_agents.py`
- [ ] 2.3 Add Pi's host config path (`~/.pi/agent`) to `_agent_host_paths(home)` in `config_agents.py`
- [ ] 2.4 Add `"pi"` to the `credential_dirs` comprehensions in `cli.py` (mirroring the `for agent in ("codex", "opencode")` loop)
- [ ] 2.5 Add `"pi"` to `_credential_dirs()` in `devcontainer.py`

## 3. Container image build

- [ ] 3.1 Add `install_pi` flag to `dockerfile.py`'s `render()` and wire it alongside `install_codex`/`install_opencode`
- [ ] 3.2 Add `{% if install_pi %}` npm install block to `Dockerfile.j2` for `@earendil-works/pi-coding-agent@0.80.6`

## 4. Credential mounting and sync

- [ ] 4.1 In `config_agents.py::sync_credentials()`, add a Pi branch gated on `host_paths["pi"].exists()` calling `_sync_generic_credentials(project_sandbox_dir, "pi", source_dir, include_files=("auth.json",))`
- [ ] 4.2 In `container_cli.py::build_mount_specs()`, add `pi_credentials_dir` param and mount it to `/project-sandbox-secrets/pi` (no `/project-sandbox-config/pi`)
- [ ] 4.3 In `container_cli.py::build_run_argv()`, forward the new `pi_credentials_dir` param through to `build_mount_specs`
- [ ] 4.4 In `devcontainer.py`, add `mount_pi_secrets` boolean and `pi_credentials_mount`, following the `opencode`-only-secrets pattern (no baked config mount)
- [ ] 4.5 In `_provision.sh.j2`, add the Pi credentials-dir copy step (`.pi/agent`), following the opencode multi-path copy pattern but for the single `agent` subdir

## 5. Entrypoint dispatch

- [ ] 5.1 Add `pi)` case arm to `entrypoint.sh.j2`: `exec pi "$@"`
- [ ] 5.2 Add `pi-headless)` case arm to `entrypoint.sh.j2`: `exec pi -p "$PROMPT" --approve "$@"`, always including `--approve`
- [ ] 5.3 Set `PI_SKIP_VERSION_CHECK=1` (and `PI_TELEMETRY=<confirmed-value>` per task 1.1) in the Pi runtime environment in the entrypoint/provision script

## 6. CLI flag handling

- [ ] 6.1 Extend headless model/effort injection logic so Pi emits a single combined `--model <model>:<effort>` flag instead of separate `--model`/`--effort` flags
- [ ] 6.2 Add `--model`/`--effort` argparse help text examples for Pi alongside the existing Codex/OpenCode examples

## 7. Provider allowlist warning

- [ ] 7.1 Generalize `_warn_opencode_provider_allowlist` (rename and/or extend condition) to trigger on `run_agent in ("opencode", "pi")` with agent-appropriate wording
- [ ] 7.2 Verify existing call sites (`cli.py` lines calling this function) pass through unchanged for both agents

## 8. Tests

- [ ] 8.1 `test_cli.py`: add Pi headless-dispatch test mirroring `test_unsupervised_opencode_uses_headless_dispatch_when_available`
- [ ] 8.2 `test_cli.py`: add `test_model_injected_for_pi_headless` and `test_effort_injected_for_pi_headless` (+ high-effort variant), asserting the combined `--model x:y` flag shape
- [ ] 8.3 `test_cli.py`: add `test_pi_effort_and_model_can_be_combined`
- [ ] 8.4 `test_cli.py`: add a provider-allowlist warning test for `--agent pi` mirroring the OpenCode warning test
- [ ] 8.5 `test_container_cli.py`: extend `test_build_run_argv_mounts_staged_agent_credentials_when_present` and `test_no_forward_credentials_omits_secrets_but_keeps_config` (or add Pi-specific siblings) to cover Pi's secrets-only mount
- [ ] 8.6 `test_renderers.py`: add Dockerfile-content assertion for `npm install -g @earendil-works/pi-coding-agent@0.80.6`
- [ ] 8.7 `test_devcontainer.py`: add Pi credential-mount test alongside the existing codex/opencode fake-credential-dir tests
- [ ] 8.8 Add regression test confirming `oauth_refresh.py` and `token_expiry.py` no-op safely for `"pi"` (unrecognized-agent path), per design's deferred-wiring decision

## 9. Docs

- [ ] 9.1 Update `README.md` to list Pi as a supported agent
- [ ] 9.2 Update `docs/usage.md` (Install / Quick Start / API Key Injection sections) to cover Pi
- [ ] 9.3 Update `docs/runtime.md` if Pi affects file layout or image tags
- [ ] 9.4 Update `docs/security.md` to note Pi's BYOK provider-allowlist warning and the deferred OAuth-refresh/token-expiry gap

## 10. Verification

- [ ] 10.1 `uv run python -m compileall src tests`
- [ ] 10.2 `uv run pytest -q`
- [ ] 10.3 `uv run project-sandbox --agent pi --dry-run ...` to confirm rendered command/mounts without starting a container
