# Generated Files and Runtime Behavior

## End-to-End Flow

Given `project-sandbox /path/to/repo python:3.12-slim`:

1. Read host `git config --global` identity.
2. Render `<project>/.project-sandbox/` with generated Dockerfiles, entrypoint,
   firewall scripts, sanitized agent configs, a local `.gitignore` safeguard, and
   a devcontainer post-start init script. Agent credentials are staged separately
   under `/tmp` with private directory permissions to reduce the chance of
   accidentally committing them.
3. Render `<project>/.devcontainer/` with symlinks back into
   `.project-sandbox/` so the Dockerfile and firewall script remain a single
   source of truth.
4. Detect available host agent configs (`~/.claude`, `~/.codex`,
   `~/.config/opencode`, `~/.pi/agent`) and install only those agent CLIs into
   the generated Dockerfile. OpenSpec and Bash are always available.
5. Append `.project-sandbox/` and `.devcontainer/` to `<project>/.gitignore`
   idempotently.

Given `project-sandbox --agent claude /path/to/repo python:3.12-slim`, it also:

1. Selects a direct-run runtime: Apple `container` on macOS, or Docker/Podman on
   Linux. Podman must be rootless; a rootful Podman is refused.
2. Verifies the Apple `container` system service is running when that runtime is
   selected.
3. Builds or reuses the image, pins it for this session, and releases the build
   context lock. CLI configs, credentials, prompts, and env files are staged in
   a unique private directory outside the checkout; persistent devcontainer
   credentials are left alone.
4. Starts the container entrypoint, which wires Git and jj identity, copies only
   the selected agent's mounted credentials into the container's home, then runs
   the firewall before exec'ing the agent. Headless modes use the same base-agent
   credential policy. `--agent bash` is intentionally multi-agent and receives
   every detected forwarded credential except Copilot's token. On exit, the
   CLI removes only its own private session input tree.

Interactive `--agent codex` launches with `--no-daemon` to bypass Codex's shared
background app server, which can fail to start inside the sandbox. Model, effort,
fast mode, and forwarded arguments still apply. Headless runs continue to use `codex exec`.
When launching Codex manually from a sandbox shell or devcontainer, use
`codex --no-daemon` as well. Both generated Codex configs set
`check_for_update_on_startup = false`; update Codex by rebuilding the image.

With `--agent pi --pi-ollama`, a runtime networking adapter is selected before
step 4. Native host-loopback forwarding is preferred; rootless Podman on Linux
with pasta forwards only the planned ports to host loopback over IPv4, and local
Linux bridge modes (Docker Engine) lease a shared project-sandbox forwarder on the
exact bridge address: one detached forwarder per bridge address and port, shared
by concurrent sessions and exiting about two seconds after the last session ends
(see [`docs/security.md`](security.md)). No external tool such as `socat` is
needed. Agent-proxy and
Ollama sessions on Apple `container` use a user-configured
`host.docker.internal` localhost DNS mapping; see the setup and restart warning
in `docs/usage.md`.

On Linux, `--runtime chroot` explicitly selects a dummy filesystem-layout
verification runtime. It uses rootless `unshare --map-root-user --mount`, bind
mounts host system directories and the normal sandbox mount set into a temporary
chroot, then runs interactive or headless Bash. It does not build an image,
configure a firewall or network namespace, install coding-agent CLIs, or provide
a security boundary. `--runtime auto` never selects it.

This requires unprivileged user namespaces, which some hardened or nested
environments restrict in ways that vary by kernel/config (e.g. AppArmor
blocking `unshare --map-root-user`, or a mount policy rejecting a bind or
fresh mount of `/proc`) — the CI e2e workflow uses `--runtime docker` instead
for this reason (see `.github/workflows/e2e.yml`).

## File Layout

```text
<project>/
|-- .gitignore                         # appended idempotently
|-- .project-sandbox/
|   |-- .gitignore                     # local safeguard
|   |-- Dockerfile                     # generated for direct CLI runs
|   |-- Dockerfile.devcontainer        # generated for devcontainer build
|   |-- entrypoint.sh                  # container PID 1
|   |-- init-firewall.sh               # CLI firewall variant
|   |-- init-firewall-devcontainer.sh  # devcontainer firewall variant
|   |-- project-sandbox-devcontainer-init
|   |-- claude/settings.json
|   |-- claude-devcontainer/settings.json
|   |-- codex/config.toml
|   |-- codex-devcontainer/config.toml
|   |-- pi/models.json                 # only with --pi-ollama
|   |-- pi/settings.json               # only with --pi-ollama
|   |-- .dockerfile-checksums.json     # trusted project Dockerfile hashes
|   `-- sessions/                      # unsupervised-mode logs
`-- .devcontainer/
    |-- devcontainer.json
    |-- Dockerfile                 -> ../.project-sandbox/Dockerfile.devcontainer
    |-- init-firewall.sh           -> ../.project-sandbox/init-firewall-devcontainer.sh
    |-- claude                     -> ../.project-sandbox/claude
    |-- claude-devcontainer        -> ../.project-sandbox/claude-devcontainer
    |-- codex                      -> ../.project-sandbox/codex
    `-- codex-devcontainer         -> ../.project-sandbox/codex-devcontainer
```

The `.project-sandbox/` and `.devcontainer/` directories are generated local
state and are ignored as a whole. Re-run `project-sandbox` after cloning, pulling
generated config changes, or refreshing devcontainer credentials. Persistent
devcontainer staging lives under `/tmp/project-sandbox-<uid>/...`; CLI runs use
separate [private session inputs](security.md#private-session-inputs) and never
refresh or purge that persistent tree. Neither root is mounted wholesale.
Generated devcontainers are intentionally
multi-agent and mount all detected credential directories. Use
`--no-forward-credentials` to render an unauthenticated devcontainer.

## Image Tags and OpenSpec

When `--image-tag` is omitted, the generated image tag is
`project-sandbox-<project-name>-<8-char sha256>:latest`. The project name is
sanitized from the resolved project directory name, and the hash is derived from
the resolved absolute path so two projects with the same directory name do not
collide. Use `--image-tag` when you need a stable external tag, want to share a
prebuilt image, or need to align with local image cleanup scripts.

Generated images install a pinned `@fission-ai/openspec` version, which puts
`openspec` on `PATH`. project-sandbox does not run `openspec init` or create
OpenSpec workspace files automatically; run OpenSpec initialization commands
inside the project only when that project should own those files.

## Workspace Masking

The generated `.project-sandbox/` directory remains on the host for image builds
and devcontainer setup, but direct runs and devcontainers mask
`/workspace/.project-sandbox` with an empty read-only bind mount. Agents still
receive the generated config, credentials, prompts, and history through their
dedicated mounts, but cannot edit the generated files through the workspace.

The `/workspace/.devcontainer` directory is masked the same way (with the same
empty read-only mount), so the agent cannot read the devcontainer's host-path
mounts and config or edit them. For direct runs the mask is added only when a
`.devcontainer` directory exists in the workspace.

History and mask sources live outside the repository in
`$XDG_STATE_HOME/project-sandbox/mounts`, defaulting to
`~/.local/state/project-sandbox/mounts`. The layout is:

```text
mounts/
|-- helpers/                       # isolated devcontainer startup helper
`-- projects/<hash-of-project-path>/
    |-- history/
    |   |-- shell/                 # bash_history is written by the container
    |   `-- claude_projects/
    `-- masks/
        |-- <session-id>/empty/    # removed at CLI session teardown
        `-- devcontainer/empty/    # retained for subsequent reopen operations
```

Each CLI session gets its own empty read-only mask. Interactive CLI sessions and
devcontainers share persistent history for the same canonical project path;
headless sessions do not mount that history. Dry-runs create no host storage.

Regenerate existing devcontainer configurations with `project-sandbox` to adopt
the private sources and startup helper. Regeneration is also required after
moving a workspace or removing the helper installation. Devcontainer startup
recreates missing history/mask directories and empty credential mount roots,
but refuses symlinks, unexpected ownership or permissions, and nonempty masks.

Existing `.project-sandbox/history` contents are left untouched and are not
automatically migrated; history in the new location starts empty. Broad extra
mounts such as the entire home directory are rejected when they expose private
mount storage. Ambiguous or unmodeled `--mount` options are also rejected; use
explicit supported bind mounts. See [implicit mount security](security.md#implicit-history-and-mask-mounts).

## Project Dockerfile Tamper Detection

When you build from a project Dockerfile supplied with `--dockerfile`, that file
lives in the writable workspace where an agent could rewrite it during a session.
After each build, project-sandbox records a SHA256 of the Dockerfile in
`.project-sandbox/.dockerfile-checksums.json` — inside the masked directory, so a
running sandbox can neither read nor alter it. On a later run the current
Dockerfile is re-hashed and compared; a mismatch prints a `[W]` warning so you can
review the change before rebuilding. Generated Dockerfiles (the `base_image` and
`--python-uv` flows) live in the masked directory and cannot be tampered with, so
they are not tracked.
