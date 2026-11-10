# Generated Files and Runtime Behavior

## End-to-End Flow

Given `project-sandbox /path/to/repo python:3.12-slim`:

1. Read host `git config --global` identity.
2. Create this session's private staging directory under the runtime root and
   render into it: the generated Dockerfile, entrypoint, firewall script and
   identity init script (`build/`), sanitized agent configs (`config/`) and
   staged agent credentials (`secrets/`). Nothing is written into the project
   (see [Where state lives](#where-state-lives)).
3. Detect available host agent configs (`~/.claude`, `~/.codex`,
   `~/.config/opencode`, `~/.pi/agent`) and install only those agent CLIs into
   the generated Dockerfile. OpenSpec and Bash are always available.

It writes no devcontainer files and never edits the project's `.gitignore`.
`project-sandbox --devcontainer /path/to/repo python:3.12-slim` renders
`<project>/.devcontainer/` instead, and exits (see
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

## Where state lives

Generated state lives outside every project, in two private per-user roots.
Every directory is created `0700` and checked before use: owned by you, not a
symbolic link, not writable by group or others. A directory that fails the
check is refused by name, never repaired or deleted. All writes go through
directory descriptors without following links, and secret files are `0600`
from their first open.

```text
$XDG_STATE_HOME/project-sandbox/        # default ~/.local/state/project-sandbox (persistent)
|-- sessions/<session-id>.json          # session records (sessions list, project-sandbox-tui)
|-- locks/<kind>-<hash>.lock            # host advisory locks
`-- projects/<name>-<hash16>/           # one per project (hash of the resolved path)
    |-- build-state.json                # image-cache fingerprint
    |-- dockerfile-checksums.json       # trusted project Dockerfile hashes
    |-- history/shell/bash_history      # interactive shell history
    |-- history/claude_projects/        # interactive Claude history
    |-- sessions/<agent>-<branch>-<ts>.log   # default headless logs (+ .md transcripts)
    |-- devcontainer-mask/              # empty; masks paths in the devcontainer
    `-- migrated                        # the legacy .project-sandbox/ was migrated

<runtime root>/                         # ephemeral, per session
`-- staging/<session-id>/
    |-- lease                           # held while the session runs
    |-- build/                          # rendered build context (Dockerfile, scripts, CA files)
    |-- config/                         # agent configs, mounted read-only
    |-- secrets/                        # staged credentials, Copilot token, api-keys.env
    |-- prompt/                         # the headless prompt
    `-- empty/                          # this session's mask source
```

The runtime root is the first of:

1. `$PROJECT_SANDBOX_RUNTIME_DIR`, when set (absolute; checked like every
   state directory);
2. `$XDG_RUNTIME_DIR/project-sandbox` (Linux), when that directory is yours
   with no group or other access;
3. `$TMPDIR/project-sandbox` under the same condition (macOS's per-user
   `/var/folders/.../T`);
4. `/tmp/project-sandbox-<uid>`.

A session mounts only its own staging and deletes it when it exits. A later
launch deletes the staging of any session whose process is gone (its lease can
be taken), so a crash or `SIGKILL` leaves nothing behind for long.

Inside the project, a launch writes only an empty `.devcontainer/` holding a
`.gitignore` of `*` (the mask target; see below), and whole-project builds
(`--python-uv`, `--rust-cargo`, `--node-npm`, `--dockerfile`) receive a
secret-free copy of the generated build inputs in `.project-sandbox/` of their
build context, written right before the build. `--devcontainer` writes its
files as real files into `.devcontainer/`:

```text
<project>/.devcontainer/
|-- .gitignore                          # "*": the directory ignores itself
|-- devcontainer.json
|-- Dockerfile
|-- Dockerfile.dockerignore             # whole-project flows only
`-- project-sandbox-devcontainer-init   # git/jj identity setup
```

Re-run `project-sandbox --devcontainer` after cloning or pulling changes that
affect the devcontainer. The generated devcontainer has no agents and mounts no
credentials.

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

`/workspace/.devcontainer` is always masked with this session's empty `empty/`
directory, read-only, so the agent cannot read, edit or create the devcontainer
config, whose `initializeCommand` runs on the host. A launch creates
`.devcontainer/` (holding only its `.gitignore`) when it is missing, so the mask
always has a target, and refuses to start when it is a symlink: masking the
link's path would leave its target writable.

A `.project-sandbox/` left in the workspace, by an older version or as a
whole-project flow's copy of the build inputs, is masked the same way when it
exists. Agents receive their config, credentials, prompts and history only
through their dedicated mounts, and every such source the CLI adds is checked
right before the container command is built: it must resolve under a state
root, not be a symlink, and be owned by you.

## Project Dockerfile Tamper Detection

When you build from a project Dockerfile supplied with `--dockerfile`, that file
lives in the writable workspace where an agent could rewrite it during a session.
After each build, project-sandbox records a SHA256 of the Dockerfile in the
project's private state (`dockerfile-checksums.json`), which no sandbox mounts, so
a running sandbox can neither read nor alter it. On a later run the current
Dockerfile is re-hashed and compared; a mismatch prints a `[W]` warning so you can
review the change before rebuilding. Generated Dockerfiles (the `base_image` and
`--python-uv` flows) are rendered fresh into private staging for every session,
so they are not tracked.
