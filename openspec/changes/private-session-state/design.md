# Design

## Context

Today every launch does `context_dir = ensure_dir(project / ".project-sandbox")` (`cli.py:667`). In branch mode with `--python-uv`/`--node-npm` it uses `workspace / ".project-sandbox"` instead (`:739`). Everything is rendered there with `write_text`:
- `Dockerfile*` and `*.dockerignore`;
- `init-firewall*.sh`, `entrypoint.sh`, `project-sandbox-devcontainer-init`, CA files, `.build-state.json`;
- agent configs (`claude/`, `codex/`, `pi/`, `opencode/`, `copilot/`, `*-devcontainer/`);
- `api-keys.env`, `prompt/`, `prompts/`, `sessions/*.log`, `history/`, `workspace-mask/`, `chroot-run.sh` and `chroot-root/`.

Other host state already lives elsewhere:
- credential staging: `/tmp/project-sandbox-<uid>/<digest>/<agent>`, keyed by project;
- host locks: `$TMPDIR/project-sandbox-<kind>-<hash>.lock`;
- session records, egress state and local-forward state: under `$XDG_STATE_HOME/project-sandbox/`.

Whole-project builds use the project (or `--docker-context`) as the build context. They `COPY {{ sandbox_copy_prefix }}<file>`, a path relative to that context, which is why `--docker-context` must contain `.project-sandbox`.

`container_cli.build_image` runs `build` with `cwd` set to the context and `.` as the context argument. Apple `container` "only reliably honors a current-directory context" (`container_cli.py:510`).

## Goals / Non-Goals

**Goals:**
- No host-generated secret, config, history, log, mask or lock inside a project. Generated build inputs leave the project too, except for whole-project flows until `private-build-context`.
- No two sessions share mutable inputs.
- One primitive for every host write, so link-safety is not re-implemented per renderer.

**Non-Goals:**
- Supplying generated files to whole-project builds from outside the project (`private-build-context`).
- Read-only agent history (`read-only-agent-history`).
- Image identity, meaning launching by image ID and a complete fingerprint. That belongs to `pin-launch-image-identity`. This change only makes the inputs per session.
- Validating user `--mount` values (`validate-all-mount-sources`).
- Devcontainer content: that belongs to `minimal-devcontainer`. This change only relocates what remains.
- Cross-user isolation beyond per-user private directories.

## Decisions

### 1. Two roots, per-project and per-session layout

```
$XDG_STATE_HOME/project-sandbox/                         persistent (0700)
  sessions/<session-id>.json                             UNCHANGED: observability records read by project-sandbox-tui
  locks/<kind>-<hash>.lock
  projects/<name>-<hash16>/                              hash of the resolved project path
    build-state.json                                     under the build lock
    history/shell/bash_history
    history/claude_projects/
    sessions/<agent>-<branch>-<ts>.log                   default --log
    migrated                                             migration marker

<runtime root>/project-sandbox[-<uid>]/                  ephemeral (0700)
  staging/<session-id>/
    lease                                                flock held for the session's lifetime
    build/            rendered context: Dockerfile*, scripts, CA files, .dockerignore
    config/           claude/ codex/ pi/ opencode/ copilot/ (mounted read-only)
    secrets/          staged credentials, copilot token, api-keys.env (0600)
    prompt/
    empty/            per-session mask source
```

The runtime root is chosen in this order:
1. `$PROJECT_SANDBOX_RUNTIME_DIR`, when set (absolute, verified private);
2. `$XDG_RUNTIME_DIR` (Linux; per-user tmpfs);
3. `$TMPDIR` when it is per-user (macOS `/var/folders/…/T`);
4. `/tmp/project-sandbox-<uid>`, verified, which is the current location.

Secrets therefore stay off persistent disk where the OS offers a per-user temporary location. The macOS `$TMPDIR` lives under `/var/folders`, which is in the default shares of Docker Desktop and Podman machine. Apple `container` shares arbitrary paths. On Colima and Lima, which share only `$HOME` by default, the mount probe fails with an error that names `PROJECT_SANDBOX_RUNTIME_DIR`. Task 1.3 also checks whether macOS's periodic cleanup of `$TMPDIR` can remove staging of a session that runs for days.

The `<session-id>` is the existing observability session ID.

**Alternative considered:** one root under `$XDG_STATE_HOME` for everything. It's simpler, but it puts staged OAuth credentials and API keys on persistent, often backed-up disk. Rejected.

### 2. A verified-directory primitive for all writes

`paths.PrivateDir` wraps an open directory descriptor:
- **`open(path, create=True)`:** walks each component from a trusted anchor (`$HOME`, the runtime root or the project root) with `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=parent)`. A missing component is created with `mkdir(name, 0o700, dir_fd=parent)`. Each one is checked with `fstat`: owned by the caller, and for state directories no group or other write.
- **`write(name, data, mode)`:** creates a temporary file with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` and mode `mode`, so a secret is `0600` from its first open. It then writes, `fsync`s, and calls `os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)`. A rename replaces a symlink at `name` rather than following it.
- **`subdir(name)`, `unlink(name)`, `rmtree()`:** all relative to the descriptor, never following links. `rmtree` is used only on directories this process created, such as session directories.

Renderers stop calling `Path.write_text`. They return their content, and the caller writes it through a `PrivateDir`. This is the bulk of the refactor (`dockerfile.py`, `firewall.py`, `config_agents.py`, `devcontainer.py` and `chroot.py`), and it makes the rendering functions pure, which simplifies tests.

`.devcontainer/` is the one lasting in-project target. Its configuration files are written only for `--devcontainer`. It is opened from a descriptor on the project root. If it is a symlink, rendering is refused, matching the review's suggestion to reject symlinked directories. Leaf symlinks inside it are replaced by the rename.

**Alternative considered:** `is_symlink()` checks before `write_text`, extending today's `_reject_symlinked_project_path`. The review explicitly notes this is racy against concurrent replacement. Rejected.

### 3. Session leases and reaping

A session takes an exclusive `flock` on `staging/<id>/lease` before staging and holds it until cleanup. On exit, it deletes its own staging directory.

At launch, under a short reaper lock, the CLI scans `staging/*`. For each one, it tries a non-blocking `flock` on `lease`. If that succeeds, the owner is dead (killed or crashed), and the directory is deleted.

This is the same pattern as the existing local-forward leases. It is immune to PID reuse, and it never unlinks a lock that someone else holds.

### 4. Build inputs

Every renderer's output goes into the session's `build/` directory. The default flow builds from there, cwd-based, as today.

Whole-project flows use the project (or `--docker-context`) as their context, and `COPY` generated files by a context-relative path. Until `private-build-context` supplies them separately, the launch copies the generated files from `build/` into `<context>/.project-sandbox/` through a `PrivateDir` opened on the context root, under the build lock, right before the build. The fingerprint is still computed over the session `build/`.

This interim copy keeps two known gaps for those flows, both closed by `private-build-context`: a file can be replaced between the copy and the build, and `--docker-context` must still contain `.project-sandbox`.

### 5. History location

`history/shell` and `history/claude_projects` move to `projects/<key>/history/`. Their mounts are unchanged: interactive sessions only, read-write. `read-only-agent-history` changes the Claude history mount afterwards.

### 6. Logs, locks and `.gitignore`

- **Session logs:** default logs go to `projects/<key>/sessions/`. The CLI prints the path at start and end, and the JSON summary includes it. `--log` is unchanged.
- **Locks:**
  - `host_locks.lock_path` moves to `$XDG_STATE_HOME/project-sandbox/locks/`, a verified `PrivateDir`;
  - `path_lock` additionally checks with `fstat` that the opened file is a regular file owned by the caller;
  - lock files are never unlinked.
- **`.gitignore`:**
  - `_update_project_gitignore` and `_write_project_sandbox_gitignore` are removed;
  - `.devcontainer/.gitignore` contains `*`;
  - existing marker lines in a project's root `.gitignore` are left in place. They are harmless, and removing them would mean editing an agent-writable file again.

### 7. `.devcontainer` is always masked

The mask exists so the agent can't edit configuration the host later executes. `devcontainer.json`'s `initializeCommand` runs on the host when the project is opened in a container. Today the mask is skipped when `.devcontainer` is absent (`cli.py:3233`), because Apple `container` rejects a bind onto a missing target. An agent can therefore create the directory and plant `devcontainer.json`.

Before building argv, the launch opens the directory mounted at `/workspace` (the session directory in isolated mode, the project in shared mode; see `vcs-session-modes`) as a `PrivateDir` anchor:
- if `.devcontainer` is absent, it creates `.devcontainer/` with `.gitignore` (`*`);
- if `.devcontainer` is a symlink, it refuses the launch;
- it then always adds the read-only mask from the session's `empty/` directory.

Refusing a symlink matters. A bind onto `/workspace/.devcontainer` would follow the link inside the container, which could leave the real directory unmasked and still let the link redirect the IDE.

An empty `.devcontainer/` holding only `.gitignore` doesn't trigger IDE "Reopen in Container" prompts, which need `devcontainer.json`.

### 8. Migration, once per project

Migration runs under the project's build lock, and only when `projects/<key>/migrated` is absent. It is postponed, with a notice, while any session record for the project is `running` with a live process: a session started by an older version still has these files mounted. Working through a `PrivateDir` opened (no-follow) on `<project>/.project-sandbox` (and `<workspace>/.project-sandbox` in branch mode):
1. Move `history/shell/bash_history` and `history/claude_projects/` into private state, by copying regular files only and skipping symlinks, then delete the originals.
2. Move `sessions/*.log` in the same way.
3. Unlink every name on the known generated-file list (leaving the build inputs a whole-project flow still copies in, see Decision 4), including secrets such as `api-keys.env`, `pi/models.json` and `opencode/opencode.json`, plus empty known subdirectories.
4. Report what was moved and removed, and list unknown entries left in place.
5. Write the `migrated` marker.

`--dry-run` performs only the scan and prints the plan.

A legacy `.project-sandbox` that still exists, holding unknown files or being recreated by a repository, is still masked in sessions with the session's `empty/` directory. That keeps the existing `agent-credential-forwarding` masking scenario true.

## Risks / Trade-offs

- [Breaking the outputs launchers depend on] → I checked project-sandbox-tui (2026-11-09). It always passes its own `--log` and `--json-summary` paths, inside its private state, for background runs. It reads `<log>.md` as the transcript and `pid`, `exit_code` and `session_id` from the summary. It reads session records directly from `$XDG_STATE_HOME/project-sandbox/sessions/*.json`. It reads nothing under `.project-sandbox/`. All of these are kept and pinned by the "Launcher-facing outputs" requirement. The per-session staging directory is named `staging/`, under a different root, so it can't be mistaken for the records directory. Other user scripts that read `.project-sandbox/sessions/*.log` get the release-note mention and the printed path.
- [`$XDG_STATE_HOME` on NFS, where `flock` is unreliable] → This is already true for egress and local-forward state. Document it. `doctor` can warn when the state root is on a network filesystem.
- [The refactor touches every renderer] → Pure renderers plus one writer make the change mechanical. Renderer tests switch from reading files to asserting returned content, which is a net simplification.
- [Devcontainer coupling] → Sequence `minimal-devcontainer` first. If it slips, this change keeps a persistent `projects/<key>/devcontainer/` staging directory for its credentials, and refers to it with absolute paths in `devcontainer.json`.

## Migration Plan

- Automatic on first launch (Decision 8). There's no rollback of the migration itself. An older version would just recreate `.project-sandbox/` on its next run, with fresh history.
- To roll back the code, revert the change. Private state is left in place and ignored by older versions.
