# TODO - outstanding items

## Implement API key injection

When running with --no-forward-credentials, we should add functionality to inject
environment variables for API keys (e.g. Bedrock / Anthropic). These credentials could e.g. 
come from the local environment on the host shell, or from a .env file.

## Optimize image build times on start

Startup can be slow when `project-sandbox` needs to build or rebuild the container
image before launching. Outstanding: profile the current image build path and
reduce cold-start and rebuild time, especially for repeated local runs where most
inputs have not changed.

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
