# Generated files and runtime behavior

## End-to-end flow

`project-sandbox /path/to/repo python:3.12-slim` generates files only:

1. Reads the host's global Git identity.
2. Renders `<project>/.project-sandbox/` (Dockerfiles, entrypoint, firewall
   scripts, sanitized agent configs, a `.gitignore` safeguard) and
   `<project>/.devcontainer/`, whose files link back into `.project-sandbox/`.
3. Installs only the agent CLIs whose host configs exist (`~/.claude`,
   `~/.codex`, `~/.config/opencode`, `~/.pi/agent`); OpenSpec and Bash are always
   available.
4. Appends `.project-sandbox/` and `.devcontainer/` to `<project>/.gitignore`.

With `--agent NAME` it also:

1. Selects a runtime: Apple `container` on macOS, Docker or Podman on Linux.
   Podman must be rootless.
2. Builds or reuses the image and pins it for the session (see
   [Concurrent sessions](#concurrent-sessions)), then stages configs,
   credentials, prompts and env files in a private per-session directory
   outside the checkout.
3. Starts the container. The entrypoint wires Git and jj identity, copies only
   the selected agent's credentials into the container's home, installs the
   firewall, and execs the agent. `--agent bash` gets every detected credential
   except Copilot's token. On exit the CLI removes its own session inputs.

Interactive Codex runs with `--no-daemon`, because its shared background app
server can fail to start in the sandbox; use `codex --no-daemon` when starting
it by hand. Generated Codex configs disable update checks; rebuild the image to
update Codex.

**Local services** (Ollama, a local agent gateway or Internet proxy) are
forwarded per runtime: native host-loopback forwarding where available; pasta
for rootless Podman on Linux (planned ports only); a leased, shared forwarder
on the bridge address for Docker Engine, which exits about two seconds after
the last session using it. Apple `container` needs the
[`host.docker.internal` alias](apple-container-dns.md).

**`--runtime chroot`** (Linux only) checks the filesystem layout with rootless
`unshare --map-root-user --mount` and runs Bash. It builds no image, configures
no firewall, installs no agents and is **not a security boundary**;
`--runtime auto` never picks it. Some hardened kernels block it, which is why CI
uses Docker.

## File layout

```text
<project>/
|-- .gitignore                         # appended idempotently
|-- .project-sandbox/                  # masked inside sessions
|   |-- Dockerfile, Dockerfile.devcontainer
|   |-- entrypoint.sh, init-firewall.sh, init-firewall-devcontainer.sh
|   |-- project-sandbox-devcontainer-init
|   |-- claude/, claude-devcontainer/, codex/, codex-devcontainer/
|   |-- pi/                            # only with --pi-ollama
|   |-- .dockerfile-checksums.json     # trusted project Dockerfile hashes
|   `-- sessions/                      # unsupervised-session logs
`-- .devcontainer/
    |-- devcontainer.json
    `-- Dockerfile, init-firewall.sh, claude*, codex*  -> ../.project-sandbox/…
```

Both directories are generated local state; re-run `project-sandbox` after
cloning or pulling. Devcontainers are multi-agent and mount every detected
credential directory, staged under `/tmp/project-sandbox-<uid>/`; use
`--no-forward-credentials` for an unauthenticated devcontainer.

History and mask sources live in private host storage,
`$XDG_STATE_HOME/project-sandbox/mounts` (default
`~/.local/state/project-sandbox/mounts`):

```text
mounts/
|-- helpers/                       # devcontainer startup helper
`-- projects/<hash-of-project-path>/
    |-- history/shell/, history/claude_projects/
    `-- masks/<session-id>/empty/, masks/devcontainer/empty/
```

Interactive sessions and devcontainers of a project share its history; headless
sessions mount none. Re-render devcontainer configurations after moving a
workspace. Old `.project-sandbox/history` contents are not migrated.

## Workspace masking

Sessions and devcontainers see `/workspace/.project-sandbox` and
`/workspace/.devcontainer` as empty read-only directories, so an agent can't
read or edit generated files or the devcontainer's host-executed configuration.
Agents still get their config, credentials, prompts and history through
dedicated mounts. In direct runs the `.devcontainer` mask is added only when the
directory exists.

## Image tags and OpenSpec

Without `--image-tag`, the tag is
`project-sandbox-<project-name>-<8-char sha256 of the path>:latest`, so projects
with the same directory name don't collide. Use `--image-tag` for a stable or
shared tag.

Images include a pinned `@fission-ai/openspec`; project-sandbox never runs
`openspec init` for you.

## Project Dockerfile tamper detection

A `--dockerfile` lives in the writable workspace. After each build its SHA-256
is recorded in `.project-sandbox/.dockerfile-checksums.json` (masked from the
sandbox); a later run that finds a different hash prints a `[W]` warning so you
can review the change before rebuilding. Generated Dockerfiles live in the
masked directory and need no tracking.

## Concurrent sessions

**Builds.** Invocations sharing a generated `.project-sandbox` directory hold
its lock from rendering through image selection, so build inputs stay stable
while the builder reads them. A second lock on the runtime's image store, shared
across projects, covers cache inspection, building and image selection; builds
on one runtime queue, sessions then run concurrently.

**Image pinning.** Each session tags its image with a private
`localhost/project-sandbox-session:<random>` alias under the image lock, checks
its content identity, and deletes it at teardown. Another project moving a
shared tag can't swap or delete the session's image. Docker and Podman launch
the image ID; Apple container launches the alias. `--no-build` pins the existing
image but doesn't check that it was built for the session's options.

**Cache reuse** needs matching generated inputs (including certificates and
ignore files), runtime, build identity, tag and image identity. Whole-project
build contexts always build, using the runtime's layer cache.

**Workspace ownership.** A `--branch` session holds an exclusive lease on its
branch/bookmark and workspace directory from setup through finalization; a
second launch for either is refused as "already in use", whatever
`--worktree-dir` or symlinked path it uses. Kept workspaces become reusable when
their session ends. Git and jj setup and finalization each take a short
repository lock. Git index locks are never removed automatically: if one
exists, the worktree is kept and the lock path reported. Stop whatever uses the
worktree, confirm the lock is stale, then remove it by hand.

These locks coordinate project-sandbox processes only, not external
devcontainer builders, manual runtime commands or your own Git use. A killed CLI
releases its locks but can leave a container running; stop it before reusing
the workspace.
