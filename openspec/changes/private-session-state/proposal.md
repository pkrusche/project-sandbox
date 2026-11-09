# Proposal

## Why

The host keeps its generated state in `<project>/.project-sandbox/`, at fixed, predictable paths inside a directory that the repository and earlier agent sessions can write. This includes:
- Dockerfiles, firewall and entrypoint scripts, CA files and build-cache state;
- agent configs, including agent-proxy bearer keys in `pi/models.json` and `opencode/opencode.json`;
- `api-keys.env`, prompts, session logs, shell and Claude history, and the workspace mask directory.

Several findings in TODO.md follow from this one placement:
- **#3:** renderers follow planted symlinks and overwrite files outside the project. The host also rewrites the agent-writable root `.gitignore`.
- **#4:** symlinked `history/` and `workspace-mask/` directories are resolved and mounted, exposing arbitrary host directories.
- **#6 (inputs half):** concurrent launches rewrite one shared build context while another build reads it. A secret written for one session can end up in another session's build context.
- **#8:** prompts, proxy configs, API-key env files and credential staging are keyed by project (or project and agent), not by session. Concurrent sessions overwrite or delete each other's inputs.
- **Hardening list:** host lock files live in world-writable `/tmp` under guessable names. Secret files are written first and restricted afterwards with `chmod`.

Guarding each write individually treats the symptom. Moving the state out of the project removes the attack surface.

## What Changes

- **A private per-user state root, outside every project.**
  - Persistent state goes under `$XDG_STATE_HOME/project-sandbox/projects/<name>-<hash>/`: build-cache state, history and session logs.
  - Per-session secrets and staging go under a per-user runtime root: `$XDG_RUNTIME_DIR/project-sandbox/` on Linux, and the per-user `$TMPDIR` on macOS.
  - Every directory is created `0700` and verified: owned by the caller, not a symlink, no group or other write.
  - All writes go through descriptors of verified directories, with no-follow semantics. Secret files are created `0600` on their first open.
- **Per-session staging.** Each session gets its own directory for:
  - the rendered build context;
  - prompt, API-key env file, agent configs and proxy keys;
  - staged credentials and the Copilot token.

  A session mounts only its own inputs, and on exit it deletes only its own directory. Directories of crashed sessions are reaped when a later launch finds that their owning process is gone.
- **Builds no longer read generated files from the project.**
  - The default flow builds from the private context.
  - Whole-project flows (`--python-uv`, `--rust-cargo`, `--node-npm`, `--dockerfile`) receive the generated files as a second, named build context on Docker and Podman.
  - Apple `container` 1.7.1 has no `--build-context`. There the generated files come from a content-addressed assets image (`COPY --from=<assets>`). A cloned private build context is the fallback. A spike decides between them, without changing this proposal's behavior.
  - `--docker-context` no longer has to contain `.project-sandbox`.
- **Masks come from fresh, private empty directories.**
  - The `/workspace/.devcontainer` mask, the Rust `target/` mask and the mask over a legacy `/workspace/.project-sandbox` all use an empty directory created per session in private state.
  - **`.devcontainer` is always hidden from sandboxes.**
    - Today it is masked only if it already exists at launch, so an agent can create `.devcontainer/devcontainer.json`, and its `initializeCommand` then runs on the host when the project is reopened in a container.
    - When `.devcontainer/` is absent, the host now creates it (empty, with its self-ignoring `.gitignore`) before launch, and masks it.
    - A symlinked `.devcontainer` is refused, because masking the link's path would leave its target writable.
  - Implicit mount sources (history, masks, staging) are validated as private, non-symlinked directories before they're mounted.
- **History is read-only by default** (user decision):
  - Claude's `~/.claude/projects` history (transcripts and auto-memory) is mounted read-only into both interactive and headless sessions, and copied into a writable scratch location in the container, so the agent can still write during the session.
  - **BREAKING:** headless sessions previously had no history and interactive sessions had it read-write.
  - The new `--persist-agent-history` flag opts in to the old read-write behavior.
  - Shell history is unchanged: interactive only, read-write, now in private state.
- **Devcontainer files live only in `.devcontainer/`.**
  - The devcontainer's generated files are written there, as real files rather than symlinks into `.project-sandbox`, through the same no-follow primitive.
  - `.devcontainer/.gitignore` contains `*`, so the directory ignores itself. **BREAKING:** the host no longer appends entries to the project's root `.gitignore`.
- **Host locks** move to `<state root>/locks/` and are checked with `fstat` (regular file, owned by the caller).
- **Migration.** On first run, the CLI moves an existing project's history and session logs into private state, without following links. It deletes the known generated files in `.project-sandbox/` (including stale secrets) through no-follow, descriptor-relative unlinks, and leaves unknown files in place with a notice. `--dry-run` reports what it would do and writes nothing.
- **BREAKING (paths):**
  - Default session logs move to `<state>/projects/<key>/sessions/`. The CLI prints the path, and `--log` still overrides it.
  - Tools that read `.project-sandbox/` directly must use the reported paths.
  - project-sandbox-tui is unaffected. It passes its own `--log` and `--json-summary` paths and reads session records from `$XDG_STATE_HOME/project-sandbox/sessions/`, and both stay unchanged and are now pinned by the spec.

## Capabilities

### New Capabilities

- `host-state-storage`: where host-generated state lives, how it is kept private and safe against link attacks, per-session isolation and cleanup, how builds receive generated inputs, implicit mount-source validation, lock files, and migration from `.project-sandbox/`.
- `session-history`: which history a sandbox sees, whether it is persisted, and the opt-in for writable agent history.

### Modified Capabilities

(none. Existing specs don't define the generated-file locations. The `agent-credential-forwarding` scenario that masks a legacy `/workspace/.project-sandbox` stays true, because the mask is kept. Any statements in that spec about where credentials are staged couldn't be checked: this session's sandbox blocks reading that file, and task 1.1 checks it.)

## Impact

- **Code:**
  - `paths.py`: new state-root, runtime-root and session-dir helpers; the verified-directory and descriptor-write primitive.
  - `cli.py`: `context_dir` becomes the per-session build dir; staging, history, masks, logs, `.gitignore`, migration.
  - Renderers write into the private directory: `dockerfile.py`, `firewall.py`, `config_agents.py`, `devcontainer.py`, `chroot.py`.
  - `build_cache.py`: state location, fingerprint over the session context.
  - `container_cli.py`: `--build-context` argv for Docker and Podman; the Apple assets-image build.
  - `session.py`: log location.
  - `host_locks.py`: lock directory and `fstat` checks.
  - `copilot_auth.py`: per-session token staging.
  - Templates: `Dockerfile.j2`'s `sandbox_copy_prefix` becomes `COPY --from=sandbox`; `entrypoint.sh.j2` seeds the history copy.
- **Depends on:**
  - `minimal-devcontainer` should land first. Without it, the devcontainer still needs persistent credential and agent-config staging. This change would then have to keep a persistent devcontainer staging directory, which is avoidable work.
  - `isolate-firewall-helpers` is independent.
- **Enables:**
  - `pin-launch-image-identity` (per-session contexts make the fingerprint well-defined);
  - `validate-all-mount-sources` (implicit sources are already validated here, leaving only `--mount` values).
- **Runtimes:** per-session staging under `$TMPDIR` or `$XDG_RUNTIME_DIR` has to be bind-mountable on each runtime: Apple `container`, Docker Desktop file sharing, Colima/Lima home-only shares, Podman machine. Unsupported locations fail with a clear message naming the path.
- **Docs:** `docs/usage.md` (paths, `--persist-agent-history`, log location), `docs/security.md`, `docs/development.md` (e2e cleanup paths), `README.md` (one line on where state lives).
