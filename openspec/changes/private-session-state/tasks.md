# Tasks

## 1. Spikes and prerequisites

- [ ] 1.1 Read `openspec/specs/agent-credential-forwarding/spec.md` and confirm it states no staging location or per-project keying that this change alters. If it does, add a MODIFIED delta for those requirements. Verify with `openspec validate private-session-state --strict`.
- [ ] 1.2 On Apple `container` 1.7.1, build a `FROM scratch` assets image from a temporary directory, then build a second image using `COPY --from=<assets tag>`. Separately, build with an absolute `-f` outside the cwd context, with a `<Dockerfile>.dockerignore` beside it. Record the results in design.md Decision 4. Mark the assets image as the primary approach, or switch to the clone fallback. Delete the spike images afterwards. Verify that design.md names exactly one Apple mechanism.
- [ ] 1.3 Probe bind-mounting a file under each runtime-root candidate (`$XDG_RUNTIME_DIR`, macOS `$TMPDIR`, `/tmp/project-sandbox-<uid>`) on Apple `container`, Docker Desktop, rootless Podman and Podman machine. Record the results in `docs/development.md`. Verify that the selection order in Decision 1 matches the results.
- [ ] 1.4 Confirm `minimal-devcontainer` has landed, or add the persistent `projects/<key>/devcontainer/` staging fallback from Risks to this task list. Verify the decision in proposal.md's Impact section.

## 2. Private directory primitive and roots

- [ ] 2.1 Implement `paths.PrivateDir` (no-follow descriptor walk, `fstat` owner and mode checks, `write` with `O_EXCL|O_NOFOLLOW` temp files and descriptor-relative `os.replace`, `subdir`, `unlink`, `rmtree`). Verify with unit tests:
  - a pre-planted symlinked component is refused;
  - a symlinked leaf is replaced and its target untouched;
  - a link swapped in between `open` and `write` (simulated with a hook) does not redirect the write;
  - a secret file is mode `0600` immediately after the open, checked before any write;
  - a group-writable or foreign-owned directory is refused (owner check simulated with a stubbed `fstat`).
- [ ] 2.2 Add state-root, runtime-root and per-project key helpers, using the selection order from Decision 1 and rejecting relative XDG values. Verify with tests for each environment combination and for a non-private `$TMPDIR`.
- [ ] 2.3 Move host locks to `$XDG_STATE_HOME/project-sandbox/locks/`, with `fstat` regular-file and owner checks. Verify with `tests/test_host_locks.py` cases for a FIFO, a foreign-owned file and a symlink at the lock path.

## 3. Session staging and leases

- [ ] 3.1 Create `<runtime root>/staging/<session-id>/` with `lease`, `build/`, `config/`, `secrets/`, `prompt/` and `empty/`. Hold the lease for the session's lifetime, and delete the directory in the session's cleanup. Verify with CLI tests that two sessions get distinct directories, and that cleanup removes only its own.
- [ ] 3.2 Add the reaper: at launch, under a reaper lock, delete session directories whose lease can be taken. Verify with a test that kills a child holding the lease (`SIGKILL`), then checks that its directory is reaped and a live session's directory is kept.
- [ ] 3.3 Move credential staging (`config_agents.sync_credentials`, `credentials_dir`), the Copilot token (`copilot_auth.stage_token`), `api-keys.env`, prompts and agent-proxy configs (`pi/models.json`, `opencode/opencode.json`) into the session's `secrets/`, `config/` and `prompt/`, and mount only those. Verify with tests for the four concurrency scenarios in `host-state-storage` ("Each session has its own staging"), including that `--no-forward-credentials` no longer purges another session's staging.

## 4. Rendering into the private build context

- [ ] 4.1 Make the renderers return content (`dockerfile.render*`, `firewall.render`, `config_agents.render`, `render_entrypoint`, `render_devcontainer_entrypoint`, `chroot`), and write their output through `PrivateDir` into the session `build/` or `config/`. Verify that the existing renderer tests pass after switching to returned content, and that a planted `.project-sandbox/init-firewall.sh` in the project is never read.
- [ ] 4.2 Move `.build-state.json` to `projects/<key>/build-state.json`, keep the build lock keyed by the project, and fingerprint the session `build/` inputs. Verify with `tests/test_build_cache.py` that two sessions with identical inputs hit the cache, and that different inputs miss.
- [ ] 4.3 Replace `sandbox_copy_prefix` with `sandbox_copy_from`. On Docker and Podman, add `--build-context sandbox=<build/>` for whole-project and `--docker-context` builds, plus `--ignorefile` on Podman. Drop the "`--docker-context` must contain `.project-sandbox`" check. Verify with argv tests per runtime and flow, and with a CLI test that a custom context without `.project-sandbox` is accepted.
- [ ] 4.4 Implement the Apple mechanism chosen in task 1.2: either a content-addressed assets image with pruning, or a cloned context. Verify with argv and flow tests, and with an opt-in container test that builds `--python-uv` on Apple `container` with a planted `.project-sandbox/entrypoint.sh` in the project, and checks that the image contains the generated entrypoint.
- [ ] 4.5 Move the chroot backend's `chroot-run.sh` and `chroot-root/` into the session `build/`. Verify that `scripts/e2e-test.sh --runtime chroot` passes.

## 5. Mounts, masks and history

- [ ] 5.1 Use the session's `empty/` directory as the source for the `.devcontainer`, Rust `target/` and legacy `.project-sandbox` masks. Validate every implicit mount source right before argv construction (under a state root, not a symlink, owned by the caller). Verify with tests on the final mount list, including that a symlinked legacy `history/shell` or `workspace-mask` is never a mount source.
- [ ] 5.1a Always mask `/workspace/.devcontainer` (design Decision 7). Create `.devcontainer/` with its `.gitignore` when absent, through `PrivateDir`, and refuse a symlinked `.devcontainer`. Verify with CLI tests for the existing, absent, symlinked and worktree cases. Add an e2e check that an in-sandbox write to `/workspace/.devcontainer/devcontainer.json` fails, on Docker and Apple `container`.
- [ ] 5.2 Move shell history to `projects/<key>/history/shell/`, mounted for interactive sessions only. Verify with CLI tests for interactive and headless sessions.
- [ ] 5.3 Mount `history/claude_projects` read-only at `/project-sandbox-history/claude_projects` for interactive and headless sessions. Seed `$HOME/.claude/projects` from it in `entrypoint.sh.j2` after the privilege drop. Add a size warning above 256 MiB. Verify with renderer tests on the seeding step, and with CLI tests on the mount mode for both session kinds.
- [ ] 5.4 Add `--persist-agent-history`: mount read-write at `$HOME/.claude/projects` and skip seeding, and show the mode in `--dry-run`. Verify with CLI tests. Add an e2e check that a memory file written without the flag does not persist, and does persist with it.

## 6. Devcontainer, logs and `.gitignore`

- [ ] 6.1 Write the devcontainer's generated files as real files in `.devcontainer/` through `PrivateDir` opened on the project root. Refuse a symlinked `.devcontainer`. Write `.devcontainer/.gitignore` with `*`, and point `devcontainer.json` at the new paths. Verify with `tests/test_devcontainer.py` cases for a symlinked leaf (replaced) and a symlinked directory (refused).
- [ ] 6.2 Remove `_update_project_gitignore` and `_write_project_sandbox_gitignore`. Verify with `tests/test_gitignore.py`, updated so that a launch leaves the root `.gitignore` byte-identical.
- [ ] 6.3 Move default session logs to `projects/<key>/sessions/`, print the path at start and end, and include it in the JSON summary. Verify with `session.default_log_path` tests and a CLI summary test.
- [ ] 6.4 Pin the launcher-facing contract with a test that mirrors project-sandbox-tui's background argv (`--log=<dir>/run.log --json-summary=<dir>/run.json`). It asserts the log, `run.md` and summary locations, the summary fields `pid`/`exit_code`/`session_id`, and the record location and fields under `$XDG_STATE_HOME/project-sandbox/sessions/`. Verify that the test passes before and after the state move.

## 7. Migration

- [ ] 7.1 Implement the one-time migration (Decision 8) under the build lock, with a `migrated` marker, a dry-run plan, symlinks skipped and unknown files reported. Verify with tests for:
  - stale secrets removed;
  - history and logs moved;
  - a symlinked `history` skipped, with its target untouched;
  - unknown files kept and named;
  - no second migration;
  - dry-run with no changes.

## 8. Docs and integration

- [ ] 8.1 Document the state layout, the runtime-root selection, `--persist-agent-history`, the log location and the migration in `docs/usage.md`. Update `docs/security.md` and `docs/development.md` (e2e cleanup paths, `scripts/e2e-cleanup.py`), and add one line to `README.md`. Remove findings #3, #4 and #8 from TODO.md, and note what's left of #6 for `pin-launch-image-identity`. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass, and that `scripts/run-e2e-tests.sh` passes on at least Docker and Apple `container`.
