# Generated Files and Runtime Behavior

## End-to-End Flow

Given `project-sandbox /path/to/repo python:3.12-slim`:

1. Read host `git config --global` identity.
2. Render `<project>/.project-sandbox/` with the generated Dockerfile, entrypoint,
   firewall script, sanitized agent configs, a local `.gitignore` safeguard, and
   the identity init script the image also carries. Agent credentials are staged
   separately under `/tmp` with private directory permissions to reduce the
   chance of accidentally committing them.
3. Detect available host agent configs (`~/.claude`, `~/.codex`,
   `~/.config/opencode`, `~/.pi/agent`) and install only those agent CLIs into
   the generated Dockerfile. OpenSpec and Bash are always available.
4. Append `.project-sandbox/` and `.devcontainer/` to `<project>/.gitignore`
   idempotently.

It writes no devcontainer files. `project-sandbox --devcontainer
/path/to/repo python:3.12-slim` renders `<project>/.devcontainer/` and
`.project-sandbox/Dockerfile.devcontainer` instead, and exits (see
[Devcontainer Flow](usage.md#devcontainer-flow)).

Given `project-sandbox --agent claude /path/to/repo python:3.12-slim`, it also:

1. Selects a direct-run runtime: Apple `container` on macOS, or Docker/Podman on
   Linux. Podman must be rootless; a rootful Podman is refused.
2. Verifies the Apple `container` system service is running when that runtime is
   selected.
3. Builds the image with the selected runtime.
4. Starts the container as root for the image's root stage, which runs the
   firewall and then drops to the `agent` user for good (see
   [Privileged setup and privilege drop](security.md#privileged-setup-and-privilege-drop)).
   The entrypoint then wires Git and jj identity and copies only the selected
   agent's mounted credentials into the container's home before exec'ing the
   agent. Headless modes use the same base-agent credential policy. `--agent bash` is intentionally multi-agent and receives
   every detected forwarded credential.

Interactive `--agent codex` launches with `--no-daemon` to bypass Codex's shared
background app server, which can fail to start inside the sandbox. Model, effort,
fast mode, and forwarded arguments still apply. Headless runs continue to use `codex exec`.
When launching Codex manually from a sandbox shell, use
`codex --no-daemon` as well. The generated Codex config sets
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
|   |-- Dockerfile.devcontainer        # only with --devcontainer
|   |-- project-sandbox-init           # root stage: firewall, then privilege drop
|   |-- entrypoint.sh                  # PID 1 after the drop (runs as agent)
|   |-- init-firewall.sh               # session firewall
|   |-- project-sandbox-devcontainer-init  # git/jj identity setup
|   |-- claude/settings.json
|   |-- codex/config.toml
|   |-- pi/models.json                 # only with --pi-ollama
|   |-- pi/settings.json               # only with --pi-ollama
|   |-- .dockerfile-checksums.json     # trusted project Dockerfile hashes
|   `-- sessions/                      # unsupervised-mode logs
`-- .devcontainer/                  # empty unless --devcontainer was run
    |-- devcontainer.json
    `-- Dockerfile                 -> ../.project-sandbox/Dockerfile.devcontainer
```

The `.project-sandbox/` and `.devcontainer/` directories are generated local
state and are ignored as a whole. Re-run `project-sandbox` after cloning, pulling
generated config changes, or refreshing credentials. Agent credential staging
lives outside the project under `/tmp/project-sandbox-<uid>/...`; that private
root is not mounted wholesale. The generated devcontainer has no agents and
mounts no credentials.

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
empty read-only mount), so the agent cannot read, edit or create the
devcontainer config, whose `initializeCommand` runs on the host. Direct runs
create an empty `.devcontainer/` when it is missing so the mask always applies,
and refuse to start when it is a symlink.

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
