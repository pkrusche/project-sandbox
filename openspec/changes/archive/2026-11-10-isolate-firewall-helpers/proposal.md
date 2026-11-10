# Proposal

## Why

The sandbox image grants the agent passwordless `sudo` for `/usr/local/bin/project-sandbox-init-firewall`. The entrypoint runs that script as root from `/workspace`. Its two Python helpers (endpoint resolution and the Internet-proxy preflight) run as `python3 -`, which puts the current directory on `sys.path`. A project file such as `ipaddress.py` or `socket.py` therefore runs as root. That file can be committed to the repository, or planted by the agent before it re-runs the `sudo` command. With `NET_ADMIN`, root can remove the firewall. This is security finding #1 in TODO.md.

The persistent `sudo` grant also means the agent can re-run firewall initialization during a session. TODO.md's networking table flags this under "Startup, reinitialization, and revocation". `docs/security.md` also says "the agent runs without root, so it cannot change those rules", which holds only while nothing reachable through that `sudo` rule can be subverted.

## What Changes

- **Privileged setup runs before the agent and cannot be re-entered.**
  - Agent containers start as root.
  - The root stage runs firewall initialization, then drops irrevocably to the `agent` user:
    - an empty capability bounding set;
    - no inheritable or ambient capabilities;
    - `no_new_privs` set.
  - Only then does it exec the normal entrypoint.
- **BREAKING (image contents):** agent images no longer contain the `agent ALL=(root) NOPASSWD: …init-firewall` sudoers rule. Images built before this change keep the old entrypoint and still have the rule. They must be rebuilt, and the build cache must not reuse them.
- **Privileged helpers never read code or configuration from the workspace.**
  - The firewall script changes to `/` first.
  - It runs every embedded Python helper in isolated mode (`python3 -I`).
  - It is started with a fixed, minimal environment: fixed `PATH`, `HOME=/root`, and no `BASH_ENV`, `ENV` or `PYTHON*` variables.
  - Root-run `curl` calls (the GitHub meta fetch, the agent-proxy preflight and the Ollama check) use `curl -q`, so no `.curlrc` is read, whatever `HOME` a future caller passes.
  - This also protects against files a *previous* session left in a persistent workspace, because the root stage runs before the current agent exists.
- **No sudoers rule in any image.** `minimal-devcontainer` lands first and removes the devcontainer's firewall, so no image needs the rule.
- **Regression coverage.**
  - Unit tests render the firewall script and run its Python helpers against a temporary directory full of shadow modules.
  - An e2e check confirms that inside a running session:
    - `sudo -n` cannot run anything;
    - `NoNewPrivs` is 1;
    - the capability bounding set is empty;
    - the firewall rules cannot be changed.
- `docs/security.md` describes the root stage and privilege drop.

## Capabilities

### New Capabilities

- `sandbox-privileges`: which code runs with root or elevated capabilities inside a sandbox container, when it runs, what it may read, and how the agent session is prevented from regaining those privileges.

### Modified Capabilities

(none: no existing spec states how firewall initialization obtains root)

## Impact

- **Templates:**
  - `Dockerfile.j2`: the sudoers rule is removed, and a new root-stage entrypoint is added.
  - `entrypoint.sh.j2`: the `sudo -n` calls are removed. The firewall now runs from the root stage.
  - `init-firewall.sh.j2`: `cd /`, `python3 -I` and the environment reset.
  - `_resolve_endpoints.py.j2`: unchanged code, now run isolated.
- **Runtime argv:** `container_cli.py` starts agent containers as root (for example `--user 0:0` or the image default). The image's `USER` stays `agent`, so an image run outside project-sandbox does not start as root. A plain host `exec` into a session runs as root on Docker, Podman and Apple `container` (the runtime reuses `--user 0:0`); `docs/security.md` says to pass `--user agent`.
- **Base images:** the privilege drop needs `setpriv` (util-linux). The build verifies it exists, and installs it on Debian-family bases where it is missing.
- **Build cache:** a template change alters the fingerprint, so existing images rebuild automatically. With `--no-build`, an image that predates the root stage is refused with a rebuild instruction. Starting such an image as root would run its old entrypoint, and the agent, as root.
- **Depends on:** `minimal-devcontainer`, which removes the devcontainer's `sudo` firewall path.
- **Chroot backend:** unaffected (not a security boundary; no firewall).
- **Docs:** `docs/security.md` (Network Firewall and sidecar sections), `docs/development.md` (new e2e check).
- **Security:** removes an agent-reachable root escalation path, and makes "the agent cannot change the firewall" an enforced property.
