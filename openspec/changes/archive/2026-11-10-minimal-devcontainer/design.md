# Design

## Context

`devcontainer.render` (`devcontainer.py:34–270`) runs today on every non-chroot, non-sidecar launch (`cli.py:832`, `_renders_devcontainer`). It writes `.devcontainer/devcontainer.json`, plus symlinks into `.project-sandbox/` for:
- `Dockerfile.devcontainer`;
- `init-firewall-devcontainer.sh`;
- `claude`/`claude-devcontainer`/`codex`/`codex-devcontainer` configs.

The rendered `devcontainer.json`:
- adds `NET_ADMIN`/`NET_RAW`;
- mounts staged credentials from `/tmp/project-sandbox-<uid>/…` and the agent configs;
- mounts shell and Claude history from `${localWorkspaceFolder}/.project-sandbox/history`;
- masks `.project-sandbox` and `.devcontainer`;
- sets agent environment variables and the proxy environment;
- recommends the Claude and Codex extensions;
- runs `sudo -n …init-firewall && …devcontainer-init` as `postStartCommand`;
- uses `initializeCommand` (`mkdir -p` on the host) to recreate missing bind sources.

`Dockerfile.devcontainer` is rendered from the same `Dockerfile.j2` as the session image, with `firewall_src_filename="init-firewall-devcontainer.sh"`. So it installs the same agent CLIs and the same sudoers rule. The devcontainer firewall variant sets `allow_host_network`, the all-ports gateway rule (`init-firewall.sh.j2:238–258`).

IDE research (2026-11-09):
- VS Code Dev Containers downloads VS Code Server on the host and copies it in, and talks to it over `docker exec` ([FAQ](https://code.visualstudio.com/docs/devcontainers/faq)).
- Dev Containers downloads extensions locally ([Remote FAQ](https://code.visualstudio.com/docs/remote/faq.md)), but some extensions fetch extra components from inside the container (the C# extension, for example).
- JetBrains downloads its IDE backend inside the container, and the download can hang or time out ([JetBrains forum](https://intellij-support.jetbrains.com/hc/en-us/community/posts/24469361017362/comments/24534960032402)). No supported way to pre-load it was found.

## Goals / Non-Goals

**Goals:**
- No privileged step, firewall or credential in the devcontainer.
- Offline by default, with a single unfiltered opt-in for IDEs that need it.

**Non-Goals:**
- Any agent workflow in the devcontainer.
- A filtered network for the devcontainer, such as an IDE-host allowlist (see Decision 2).
- Node/npm devcontainer support, or Codespaces and other remote services.

## Decisions

### 1. `--network none` instead of a deny-all firewall

`runArgs` gets `--network none`. The container then has only loopback, which the kernel enforces without any capabilities, script or root step.

**Alternative considered:** keep the firewall with an empty allowlist. That needs `NET_ADMIN`, a root step and `sudo` (finding #1's surface) to achieve less than `--network none` does. Rejected.

### 2. The opt-in is unfiltered runtime networking, not an allowlist

`--devcontainer-network=runtime` drops the `--network` override and gives the devcontainer the runtime's default network. It applies the `--internet-proxy` environment when one is configured, and prints a warning.

An IDE-host allowlist (VS Code update and marketplace hosts, JetBrains download hosts) would need the firewall, capabilities and `sudo` again, plus a host list that tracks IDE vendors' CDNs. Since no agent runs in the devcontainer, its trust level is the user's own development environment. An honest "unfiltered" opt-in is simpler, and doesn't pretend to be a boundary.

The flag is a command-line argument rather than an edit to `devcontainer.json`, because the file is rewritten by every `--devcontainer` run.

### 3. One Dockerfile template with an explicit devcontainer profile

`dockerfile.render` passes `devcontainer=True` for the devcontainer render instead of only swapping `firewall_src_filename`. In that profile, `Dockerfile.j2`:
- sets all `install_*` flags to false, while keeping `openspec` and the base toolchain;
- omits the firewall `COPY`, the sudoers rule and `ca_certificate_sources` runtime trust (no network). Build-time CA handling (`build_ca_certificate_sources`) is kept, because the IDE's image build may still go through a corporate proxy;
- keeps the dependency stages and the `agent` user, so `remoteUser` and file ownership on `/workspace` are unchanged.

`firewall.render` stops producing `init-firewall-devcontainer.sh`. The `allow_host_network` template branch is deleted, which closes the networking table's "Devcontainer gateway" row.

### 4. Provisioning split

`_provision.sh.j2` becomes `_provision_identity.sh.j2` (git/jj identity) and `_provision_agents.sh.j2` (config and secret copies). `entrypoint.sh.j2` includes both. `devcontainer-entrypoint.sh.j2` includes only the identity part. `postStartCommand` becomes `/usr/local/bin/project-sandbox-devcontainer-init`, which runs as `agent`.

### 5. What `devcontainer.json` keeps

- `build` (Dockerfile and context), `workspaceMount`, `workspaceFolder`, `remoteUser: agent`, `hostRequirements` and `--memory`/`--cpus`.
- `remoteEnv` git identity.
- The shell-history mount and `HISTFILE`. Its location stays where it is until `private-session-state` moves it.
- User `--mount` values, followed by the two masks, which stay last as today.
- `initializeCommand`, now only for the history and mask directories.
- `containerEnv`: `HISTFILE`, `NODE_OPTIONS` and, in `none` mode, the offline variables.

Removed: all `/project-sandbox-config` and `/project-sandbox-secrets` mounts, Claude history, `CLAUDE_*`/`CODEX_HOME`/`PI_*`/`OPENSPEC_TELEMETRY`, the agent extensions, `--cap-add`, and the firewall in `postStartCommand`.

`config_agents.render` drops the `claude-devcontainer`/`codex-devcontainer` profiles. `sync_credentials` drops the `claude-devcontainer` staging.

### 6. Rendering on request

`_renders_devcontainer` and the render call in the launch path are removed. `--devcontainer` is a terminal mode like `--build-only`:
- it validates the build options, renders `.devcontainer/` in the project directory, prints the path and the network mode, and exits;
- it never builds an image, stages credentials or starts a container;
- it ignores `--egress`, and is rejected with `--agent`, a prompt, `--branch`, `--build-only` and `--runtime chroot`;
- `--devcontainer --dry-run` prints what would be written.

`--devcontainer-network` without `--devcontainer` is a usage error.

**Alternative considered:** keep rendering on every launch. The files are a shared, in-project target that concurrent launches overwrite with their own options, for a feature most launches don't use. Rejected.

## Risks / Trade-offs

- [The IDE can't attach offline] → Task 1.1, 2026-11-09, tested VS Code 1.145.2 with Dev Containers 0.463.0 on Docker Desktop 29.9.3. The fixture was a hand-rendered `devcontainer.json` with `"runArgs": ["--network", "none"]` and the offline variables, for a `python:3.12-slim` + uv project. VS Code built the image (build-time network is unaffected), started the container with network mode `none`, and installed its server and the Python, debugpy, Pylance and Python Environments extensions into it. Running a pytest test and debugging it with a breakpoint both worked. VS Code fetches the server and extensions on the host and copies them in, so offline attach needs no network inside the container.
- [JetBrains IDEs] → Not tested (user decision, 2026-11-09: VS Code support is enough). Its backend downloads inside the container (Context), so it probably can't start offline. The docs say so and name `--devcontainer-network=runtime` for JetBrains.
- [Extensions that download components inside the container at runtime, such as C#/.NET or some debuggers] → None were seen with the Python stack. The fallback is `--devcontainer-network=runtime`; the docs list known cases as they are reported.
- [Tests that rely on host services, such as a database on the host] → They now fail. That's intended for the default. `runtime` mode restores host reachability.
- [Users who ran agents in the devcontainer] → **BREAKING**. The docs point to direct sessions (`project-sandbox --agent …`) and the TUI.
- [Users who relied on the devcontainer appearing by itself] → **BREAKING**. The docs and release notes name `--devcontainer`. Existing `.devcontainer/` files from older versions stay until the user re-renders or deletes them.

## Migration Plan

The user runs `--devcontainer` once to regenerate `.devcontainer/` with the reduced config, and rebuilds the devcontainer in their IDE. Until then, the files from the older version stay as they are. Stale `claude-devcontainer`/`codex-devcontainer` symlinks in `.devcontainer/` are removed by the renderer, and stale profile directories in `.project-sandbox/` are left for `private-session-state`'s migration.
