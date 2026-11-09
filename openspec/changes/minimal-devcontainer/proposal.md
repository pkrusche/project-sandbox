# Proposal

## Why

The generated devcontainer is configured as a second agent sandbox, on every non-chroot, firewall-mode launch:
- all agent CLIs are installed, and staged credentials for Claude, Codex, OpenCode and Pi are mounted;
- `CLAUDE_*`/`CODEX_HOME` are set, and the Claude and Codex VS Code extensions are added;
- Claude history is mounted read-write;
- the firewall runs from `postStartCommand` through a passwordless `sudo` rule with `NET_ADMIN`/`NET_RAW`;
- a firewall variant admits every port and protocol to and from the host gateway. That variant also comes before the DNS drops (TODO.md networking table, "Devcontainer gateway").

The devcontainer's purpose is narrower: a place to run and debug the project's tests with all dependencies installed. No agent runs there, and it doesn't need the Internet. Its broad setup adds attack surface for no benefit:
- the `sudo` path behind finding #1;
- the gateway exception;
- persistent credential staging that `private-session-state` would otherwise have to keep.

It is also regenerated into the project on every launch, whether or not the user wants a devcontainer. Concurrent launches overwrite each other's version, and the last launch's options win.

## What Changes

- **Rendering is an explicit request** (user decision, 2026-11-09).
  - `--devcontainer` renders `.devcontainer/` for the project from the given build options (flow, base image, `--mount` values, limits), and exits without starting a session.
  - **BREAKING:** ordinary launches no longer write devcontainer files. They only make sure an empty `.devcontainer/` exists so it can be masked (`private-session-state`).
  - `--devcontainer` is rejected together with `--agent`, a prompt, `--branch` and `--build-only`.
- **No network by default.**
  - The devcontainer runs with `--network none`, without `NET_ADMIN`/`NET_RAW`, without a firewall, and without `postStartCommand` firewall initialization.
  - The devcontainer image has no sudoers rule.
  - The offline variables of the generated toolchains (`UV_OFFLINE=1`, `CARGO_NET_OFFLINE=true`, `npm_config_offline=true`) are set, so tools fail fast instead of timing out.
  - **BREAKING:** code in the devcontainer can no longer reach the network or host services.
- **An opt-in network** for IDEs or workflows that need one.
  - `--devcontainer --devcontainer-network=runtime` uses the runtime's default network, unfiltered, with no firewall and no added capabilities. It applies the `--internet-proxy` environment when one is configured.
  - The command prints a warning that the devcontainer's network is unfiltered.
  - `--devcontainer-network=none` is the default.
- **No agents.**
  - The devcontainer image installs no agent CLIs.
  - `devcontainer.json` mounts no credentials or agent configs, sets no agent environment variables, adds no agent IDE extensions and mounts no Claude history.
  - The `claude-devcontainer`/`codex-devcontainer` config profiles and the devcontainer credential staging are removed.
  - **BREAKING:** agents can no longer be used inside the generated devcontainer. Use a direct `project-sandbox --agent …` session.
- **Kept:**
  - the same base image and project dependencies (python-uv, rust-cargo, Dockerfile flows);
  - git identity;
  - persisted shell history;
  - user `--mount` values;
  - the read-only masks over `/workspace/.devcontainer` and `/workspace/.project-sandbox`. Test code in the devcontainer may come from an agent session, and must not be able to rewrite `devcontainer.json`, whose `initializeCommand` runs on the host.
- **Independent of egress mode.** Without a network, the devcontainer no longer depends on firewall mode. `--devcontainer` ignores `--egress`.

## Capabilities

### New Capabilities

- `devcontainer-environment`: what the generated devcontainer contains and permits. This covers how it is requested, its network modes, privileges, absence of agents and credentials, retained developer conveniences and masks.

### Modified Capabilities

(none. No existing spec covers the devcontainer.)

## Impact

- **Code:**
  - `devcontainer.py`: rewritten around the reduced config.
  - `dockerfile.py`: the devcontainer render disables agent installs and the firewall `COPY`.
  - `firewall.py`: the `init-firewall-devcontainer.sh` render and the `allow_host_network` path are removed.
  - `templates/init-firewall.sh.j2`: the host-gateway block is removed.
  - `templates/Dockerfile.j2`: devcontainer branches.
  - `templates/devcontainer-entrypoint.sh.j2` and `_provision.sh.j2`: split into identity-only and agent provisioning.
  - `config_agents.py`: the `*-devcontainer` profiles are removed.
  - `cli.py`: the `--devcontainer` command path replaces `_renders_devcontainer`; the new `--devcontainer-network` flag.
- **Interplay with `isolate-firewall-helpers`:** this change lands first and deletes the devcontainer's `sudo` path. `isolate-firewall-helpers` then removes the sudoers rule from every image.
- **Enables `private-session-state`:** no persistent devcontainer staging remains.
- **IDE compatibility:** VS Code installs its server and extensions from the host side and works offline, including running and debugging tests (task 1.1). JetBrains downloads its IDE backend inside the container, so it is expected to need `--devcontainer-network=runtime`. It is not tested (user decision: VS Code support is enough).
- **Docs:**
  - `docs/usage.md` "Devcontainer Flow": test and debug environment, no agents, offline, the new flag;
  - `docs/security.md`: credential, gateway and firewall-variant paragraphs;
  - `README.md` feature line.
- **Out of scope:**
  - Node/npm devcontainer support (the missing `node_modules` tmpfs and `--shm-size`), which stays unsupported as today;
  - remote devcontainer services such as Codespaces.
