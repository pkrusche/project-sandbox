# Tasks

## 1. Spikes and prerequisites

- [ ] 1.1 Read `openspec/specs/agent-credential-forwarding/spec.md` and confirm it states no staging location or per-project keying that this change alters. If it does, add a MODIFIED delta for those requirements. Verify with `openspec validate private-session-state --strict`.
- [ ] 1.3 Probe bind-mounting a file under each runtime-root candidate (`$XDG_RUNTIME_DIR`, macOS `$TMPDIR`, `/tmp/project-sandbox-<uid>`) on Apple `container`, Docker Desktop, rootless Podman and Podman machine. Include Colima with `PROJECT_SANDBOX_RUNTIME_DIR` under `$HOME`, and check whether macOS cleans `$TMPDIR` entries of a session older than three days. Record the results in `docs/development.md`. Verify that the selection order in Decision 1 matches the results.
- [ ] 1.4 Confirm `minimal-devcontainer` has landed, or add the persistent `projects/<key>/devcontainer/` staging fallback from Risks to this task list. Verify the decision in proposal.md's Impact section.

## 2. Private directory primitive and roots

- [ ] 2.1 Implement `paths.PrivateDir` (no-follow descriptor walk, `fstat` owner and mode checks, `write` with `O_EXCL|O_NOFOLLOW` temp files and descriptor-relative `os.replace`, `subdir`, `unlink`, `rmtree`). Verify with unit tests:
  - a pre-planted symlinked component is refused;
  - a symlinked leaf is replaced and its target untouched;
  - a link swapped in between `open` and `write` (simulated with a hook) does not redirect the write;
  - a secret file is mode `0600` immediately after the open, checked before any write;
  - a group-writable or foreign-owned directory is refused (owner check simulated with a stubbed `fstat`).
- [ ] 2.2 Add state-root, runtime-root and per-project key helpers, using the selection order from Decision 1 (including `PROJECT_SANDBOX_RUNTIME_DIR`) and rejecting relative values. Verify with tests for each environment combination, a non-private `$TMPDIR`, and a relative or non-private override.
- [ ] 2.3 Move host locks to `$XDG_STATE_HOME/project-sandbox/locks/`, with `fstat` regular-file and owner checks. Verify with `tests/test_host_locks.py` cases for a FIFO, a foreign-owned file and a symlink at the lock path.

## 3. Session staging and leases

- [ ] 3.1 Create `<runtime root>/staging/<session-id>/` with `lease`, `build/`, `config/`, `secrets/`, `prompt/` and `empty/`. Hold the lease for the session's lifetime, and delete the directory in the session's cleanup. Verify with CLI tests that two sessions get distinct directories, and that cleanup removes only its own.
- [ ] 3.2 Add the reaper: at launch, under a reaper lock, delete session directories whose lease can be taken. Verify with a test that kills a child holding the lease (`SIGKILL`), then checks that its directory is reaped and a live session's directory is kept.
- [ ] 3.3 Move credential staging (`config_agents.sync_credentials`, `credentials_dir`), the Copilot token (`copilot_auth.stage_token`), `api-keys.env`, prompts and agent-proxy configs (`pi/models.json`, `opencode/opencode.json`) into the session's `secrets/`, `config/` and `prompt/`, and mount only those. Verify with tests for the four concurrency scenarios in `host-state-storage` ("Each session has its own staging"), including that `--no-forward-credentials` no longer purges another session's staging.

## 4. Rendering into the private build context

- [ ] 4.1 Make the renderers return content (`dockerfile.render*`, `firewall.render`, `config_agents.render`, `render_entrypoint`, `render_devcontainer_entrypoint`, `chroot`), and write their output through `PrivateDir` into the session `build/` or `config/`. Verify that the existing renderer tests pass after switching to returned content, and that a planted `.project-sandbox/init-firewall.sh` in the project is never read.
- [ ] 4.2 Move `.build-state.json` to `projects/<key>/build-state.json`, keep the build lock keyed by the project, and fingerprint the session `build/` inputs. Verify with `tests/test_build_cache.py` that two sessions with identical inputs hit the cache, and that different inputs miss.
- [ ] 4.3 For whole-project flows, copy the generated files from the session `build/` into `<context>/.project-sandbox/` through `PrivateDir`, under the build lock, right before the build (design Decision 4). Verify with tests that a symlinked `.project-sandbox` or leaf in the context is never followed, and that no secret file is among the copied names.
- [ ] 4.5 Move the chroot backend's `chroot-run.sh` and `chroot-root/` into the session `build/`. Verify that `scripts/e2e-test.sh --runtime chroot` passes.

## 5. Mounts, masks and history

- [ ] 5.1 Use the session's `empty/` directory as the source for the `.devcontainer`, Rust `target/` and legacy `.project-sandbox` masks. Validate every implicit mount source right before argv construction (under a state root, not a symlink, owned by the caller). Verify with tests on the final mount list, including that a symlinked legacy `history/shell` or `workspace-mask` is never a mount source.
- [ ] 5.1a Always mask `/workspace/.devcontainer` (design Decision 7). Create `.devcontainer/` with its `.gitignore` when absent, through `PrivateDir`, and refuse a symlinked `.devcontainer`. Verify with CLI tests for the existing, absent, symlinked and isolated-session cases. Add an e2e check that an in-sandbox write to `/workspace/.devcontainer/devcontainer.json` fails, on Docker and Apple `container`.
- [ ] 5.2 Move shell history to `projects/<key>/history/shell/`, mounted for interactive sessions only. Verify with CLI tests for interactive and headless sessions.
- [ ] 5.3 Move Claude history to `projects/<key>/history/claude_projects/`, with its mount unchanged (interactive only, read-write). Verify with CLI tests for interactive and headless sessions.

## 6. Devcontainer, logs and `.gitignore`

- [ ] 6.1 For `--devcontainer`, write the devcontainer's generated files as real files in `.devcontainer/` through `PrivateDir` opened on the project root. Refuse a symlinked `.devcontainer`. Write `.devcontainer/.gitignore` with `*`, and point `devcontainer.json` at the new paths. Verify with `tests/test_devcontainer.py` cases for a symlinked leaf (replaced) and a symlinked directory (refused).
- [ ] 6.2 Remove `_update_project_gitignore` and `_write_project_sandbox_gitignore`. Verify with `tests/test_gitignore.py`, updated so that a launch leaves the root `.gitignore` byte-identical.
- [ ] 6.3 Move default session logs to `projects/<key>/sessions/`, print the path at start and end, and include it in the JSON summary. Verify with `session.default_log_path` tests and a CLI summary test.
- [ ] 6.4 Pin the launcher-facing contract with a test that mirrors project-sandbox-tui's background argv (`--log=<dir>/run.log --json-summary=<dir>/run.json`). It asserts the log, `run.md` and summary locations, the summary fields `pid`/`exit_code`/`session_id`, and the record location and fields under `$XDG_STATE_HOME/project-sandbox/sessions/`. Verify that the test passes before and after the state move.

## 7. Migration

- [ ] 7.1 Implement the one-time migration (Decision 8) under the build lock, with a `migrated` marker, a dry-run plan, symlinks skipped, unknown files reported, and postponement while an older session of the project is running. Verify with tests for:
  - a running older session (migration postponed with a notice, files untouched);
  - stale secrets removed;
  - history and logs moved;
  - a symlinked `history` skipped, with its target untouched;
  - unknown files kept and named;
  - no second migration;
  - dry-run with no changes.

## 8. Docs and integration

- [ ] 8.1 Document the state layout, the runtime-root selection and its override, the log location and the migration in `docs/usage.md`. Update `docs/security.md` and `docs/development.md` (e2e cleanup paths, `scripts/e2e-cleanup.py`), and add one line to `README.md`. Remove findings #3, #4 and #8 from TODO.md, and note what's left of #6 for `private-build-context` and `pin-launch-image-identity`. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass, and that `scripts/run-e2e-tests.sh` passes on at least Docker and Apple `container`.
