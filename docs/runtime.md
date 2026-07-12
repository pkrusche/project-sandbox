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
   Linux.
2. Verifies the Apple `container` system service is running when that runtime is
   selected.
3. Builds the image with the selected runtime.
4. Starts the container entrypoint, which wires Git and jj identity, copies
   staged credentials into the container's home, then runs the firewall before
   exec'ing the agent.

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
generated config changes, or refreshing credentials. Agent credential staging
lives outside the project under `/tmp/project-sandbox-<uid>/...`.

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
