# TODO - outstanding items for next release

## GitHub Copilot agent (follow-ups from add-copilot-agent-support)

- Keychain token staging: stage the Copilot token from the macOS Keychain
  (where `copilot login` stores it) as a third source after pass and `gh`.
- Self-hosted GitHub Enterprise Server (GHES) hosts for `--copilot-host`
  (currently rejected): needs its own host/endpoint model and firewall entries.
- Opt-in GitHub MCP server: a flag to re-enable Copilot's built-in GitHub MCP
  server (now always `--disable-builtin-mcps`) for users who accept that it acts
  on GitHub with the Copilot token.
- Tighten the Copilot transcript renderer and exit reclassification from real
  e2e captures (`scripts/e2e-copilot.py --capture-dir`): replace the synthetic
  JSONL fixtures in `tests/test_transcript.py` with captured ones, and confirm
  the record kinds in `transcript.py` and
  `observability.copilot_json_stream_failure`.
- Re-check the `@github/copilot` pin with `scripts/update-pins.py`: 1.0.98 was
  taken from a web search because the npm registry was unreachable when it was
  added.
- Trim the firewall-mode Copilot host list (`init-firewall.sh.j2`): the
  IDE-extension hosts (`copilot-proxy`, `origin-tracker`,
  `copilot-telemetry.githubusercontent.com`, `collector.github.com`,
  `default.exp-tas.com`) were unused by the Copilot CLI in the `copilot-egress`
  capture (add-squid-egress-sidecar design.md Decision 5), and
  `copilot-telemetry.githubusercontent.com` is a telemetry endpoint we do not
  want reachable. Copilot CLI telemetry (`telemetry.<plan>.githubcopilot.com`)
  is already blocked in both modes on purpose (docs/security.md).

## Egress sidecar (follow-ups from add-squid-egress-sidecar)

- Let concurrent Apple container sessions share one `--internet-proxy` relay; today the fixed, firewall-baked relay port makes a second session using the same proxy fail. Resolved for `--egress sidecar` by global-egress-sidecar-macos: the global sidecar owns one detached host relay that every sidecar sandbox shares (docs/egress-sidecar.md#parent-proxy). Firewall mode is unchanged; a shared relay for it is on the ROADMAP.
- Sidecar egress for devcontainers. Sidecar launches currently leave
  `.devcontainer/` untouched; devcontainers stay firewall-only.
- Make `--egress sidecar` the default once it has had wider use.
- Remote `--agent-proxy` in sidecar mode (rejected today): the sandbox has no
  route to it. Either allowlist the gateway host in Squid (port 443 only) or
  forward it like local services.
- `--copilot-host` GHE.com tenants in sidecar mode (rejected today): capture the
  tenant host set with `scripts/e2e-copilot-egress.py` and add a tenant profile.
- Capture the business/enterprise Copilot hosts (`api.{business,enterprise}.
  githubcopilot.com` are admitted by analogy with the individual-plan capture).
- Verify local-service forwarding through the sidecar on Linux rootless Podman
  with the pasta strategy (loopback-only host services). The `egress-sidecar`
  e2e suite passed on a rootless Podman machine on macOS (netavark), which does
  not use pasta.
