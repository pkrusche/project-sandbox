# TODO — outstanding items

## OAuth refresh token handling

E.g. Claude Code uses OAuth and may write an updated refresh token back to its config
directory during a session. Because the staged credential directory is mounted
read-only inside the container, this write fails silently. The host copy of the
token is never updated, so when Claude Code rotates the token on Anthropic's side
the user is logged out on the host — they must re-authenticate and re-run
`project-sandbox` to restage fresh credentials. This problem definitely applies
to Claude Code but might also affect others. 

Outstanding: find a solution that allows the container to persist a new refresh
token without compromising the read-only mount security model. Options to evaluate:
- Creating & passing an oauth token using claude setup-token or similar
- Mount only the specific token file (e.g. `credentials.json`) read-write while
  keeping the rest of the credential directory read-only.
- Use a sidecar process or entrypoint hook that copies a written token back to the
  host staging directory at container exit.
- Intercept the token write via a writable overlay on top of the RO mount and flush
  it back to the host on clean shutdown.
- Evaluate whether Claude Code exposes an environment variable or flag to redirect
  token storage to a separate writable path.

Any solution must not expose the credential staging directory as generally writable
from inside the container.

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
