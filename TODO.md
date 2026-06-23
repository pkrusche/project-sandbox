# TODO — outstanding items

## Optimize image build times on start

Startup can be slow when `project-sandbox` needs to build or rebuild the container
image before launching. Outstanding: profile the current image build path and
reduce cold-start and rebuild time, especially for repeated local runs where most
inputs have not changed.

## Refactor documentation

Move all detailed features and functionality from completed TODO items or in README to a
docs subfolder. Create a table of contents, and group by functionality, developer notes,
etc. The README should show only a minimal set of items: a) summary of the project, b) 
quick-start instructions, c) link to documentation, d) license.

## OAuth refresh token handling — resolved (host-side refresh + start-of-session warning)

**Resolved.** The container is never relied on to refresh its own token (which is
racy under parallel agents, since OAuth refresh tokens are single-use). Instead, two
host-side measures keep the forwarded token valid as long as possible and make any
shortfall visible:

- **Host pre-launch refresh by delegation.** `oauth_refresh.refresh_host_token`
  runs the agent's own CLI on the host before staging — `claude auth status` /
  `codex login status` — so the tool refreshes and persists its token with its own
  maintained logic (file or macOS Keychain for Claude; `auth.json` for Codex). The
  freshly-persisted credential is then staged. Runs under a per-agent `flock` (so
  concurrent launches don't race on the single-use refresh token), best-effort
  (never blocks a launch), skipped by `--no-token-refresh`. bash refreshes the
  claude token; opencode has no delegated refresh. No undocumented endpoint or
  credential-store writing of our own.
- **Start-of-session warning.** `cli._warn_forwarded_credential_lifetime` reads the
  staged token's expiry (`token_expiry.py`: Claude `claudeAiOauth.expiresAt`; Codex
  access-token JWT `exp`) and prints the remaining lifetime + a note that if the
  session outlives it the in-container agent refreshes using the host's single-use
  refresh token, which rotates it and **logs the user out on the host** (the session
  itself keeps working) — so they must re-authenticate on the host and re-run.
  Sessions are **not** killed. For OpenCode it reads `~/.local/share/opencode/auth.json`
  and reports the soonest-expiring OAuth provider; long-lived/API-key providers (e.g.
  github-copilot, `expires: 0`) carry no host-logout risk and stay silent.
- `--no-forward-credentials` reads/stages/mounts no host credentials, purges any
  previously staged for the project (`config_agents.purge_staged_credentials`), and
  renders a credential-free devcontainer (`devcontainer.render(forward_credentials=False)`);
  it also disables the refresh and the warning. `--no-token-refresh` disables only the
  host refresh.

Tests: `tests/test_token_expiry.py`, `tests/test_oauth_refresh.py` (delegation:
dry-run/unknown-agent/missing-CLI/missing-config-dir no-ops, correct command per
agent, failure swallowed), `tests/test_credential_warning.py`, the per-agent
host-refresh gating cases in `tests/test_cli.py`, and the `--no-forward-credentials`
case in `tests/test_container_cli.py`. A suite-wide autouse fixture in
`tests/conftest.py` stubs `oauth_refresh.refresh_host_token` so no test shells out to
the real CLI. README documents this under "OAuth token lifetime".

Outstanding follow-ups:

Must validate on a real host:
- Confirm `claude auth status` actually triggers a refresh-and-persist for a
  near-expiry token (vs. only reading state). If it only reads, switch the Claude
  trigger to one that does refresh (e.g. a minimal `claude -p`, or `claude
  setup-token` long-lived tokens). The command lives in one place: `_AGENTS` in
  `oauth_refresh.py`.
- Confirm `codex login status` refreshes `auth.json` in place (OpenAI's CI/CD docs
  say running Codex refreshes it; verify `login status` specifically does).
- The refresh shells out to the agent CLI on the host (network + process startup);
  confirm latency is acceptable and that neither command can prompt interactively
  (we run with a 30s timeout and captured output).

Coverage gaps / not yet implemented:
- OpenCode host refresh: not done. OpenCode exposes no `auth status`/`refresh`
  delegation command (only `auth list`/`login`/`logout`), its `auth.json` is
  multi-provider, and Anthropic OAuth is being removed from OpenCode for legal
  reasons — so a clean delegated refresh isn't available. The lifetime *warning* is
  implemented (`token_expiry._opencode_expiry`); only the host pre-launch refresh is
  missing for OpenCode OAuth providers.
- GitHub Copilot CLI is not a project-sandbox agent and needs no host refresh: its
  long-lived OAuth token mints short-lived (~30 min) session tokens without being
  consumed, so an in-container refresh does not log out the host.
- Devcontainer sessions are out of scope: they mount the same staged credentials but
  are launched by the IDE, so neither the host refresh nor the warning applies. A
  long-lived devcontainer can still rotate-then-lose a token like the original bug.
  Consider applying the same delegated refresh + warning to that path.

## Telemetry and config filtering for OpenCode and GitHub Copilot

OpenCode credentials are staged as-is from `~/.config/opencode` with no sanitization
equivalent to the Claude/Codex config generation. This means auto-update settings,
telemetry endpoints, and other security-relevant config keys in the user's opencode
config are passed through unmodified.

Similarly, when `--allow-github` is set, `copilot-telemetry.githubusercontent.com` and
`collector.github.com` are on the firewall allowlist (needed for Copilot to function)
but there is no filtering of Copilot-related settings in the generated config.

Outstanding:
- Generate a sanitized `opencode/opencode.json` in `.project-sandbox/` (similar to
  `claude/settings.json`) that disables auto-update and telemetry, and mount it instead
  of the raw user config.
- Evaluate whether Copilot-specific config keys (e.g. telemetry opt-out) can be
  injected into the generated OpenCode config or via environment variables.
- Update the threat model in README.md once mitigations are in place.

## Firewall: verify multi-resolver rules on a real iptables host
  
Code is **complete**: `init-firewall.sh.j2` now collects all IPv4/IPv6
nameservers via `mapfile` while preserving resolver NAT rules (no more
`{print $2; exit}` for DNS), with a `127.0.0.11` fallback; README documents the
pre-resolve-then-block DNS behavior; `tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`
covers the rendered script. The unit tests are render-only by policy and do
**not** exercise live iptables. Outstanding: run the rendered script on a host
with iptables (multiple `nameserver` entries in `resolv.conf`) and confirm
allowlisted-domain pre-resolution works across the resolver setup and that
post-firewall DNS egress does not leak before treating this as shipped — it is
the network security boundary.
