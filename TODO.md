# TODO - outstanding items

## Optimize image build times on start

Startup can be slow when `project-sandbox` needs to build or rebuild the container
image before launching. Outstanding: profile the current image build path and
reduce cold-start and rebuild time, especially for repeated local runs where most
inputs have not changed.

## OAuth refresh validation follow-ups

Resolved behavior is documented in `docs/security.md`. Remaining real-host
validation:

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
- OpenCode host refresh: not done. OpenCode exposes no `auth status`/`refresh`
  delegation command (only `auth list`/`login`/`logout`), its `auth.json` is
  multi-provider, and Anthropic OAuth is being removed from OpenCode for legal
  reasons - so a clean delegated refresh isn't available. The lifetime *warning* is
  implemented (`token_expiry._opencode_expiry`); only the host pre-launch refresh is
  missing for OpenCode OAuth providers.
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
- Update the threat model in `docs/security.md` once mitigations are in place.

## Firewall: verify multi-resolver rules on a real iptables host

Code is complete and the render path is covered by
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`.
The unit tests are render-only by policy and do not exercise live iptables.
Outstanding: run the rendered script on a host with iptables and multiple
`nameserver` entries in `resolv.conf`, then confirm allowlisted-domain
pre-resolution works across the resolver setup and post-firewall DNS egress does
not leak before treating this as shipped.
