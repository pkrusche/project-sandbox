# TODO - outstanding items for next release

## GitHub Copilot agent (follow-ups from add-copilot-agent-support)

- Keychain token staging: stage the Copilot token from the macOS Keychain
  (where `copilot login` stores it) as a third source after pass and `gh`.
- Devcontainer support for `--agent copilot`. Needs a token source that does not
  go stale, since a token captured when the devcontainer is generated would
  expire.
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

## Egress sidecar (follow-ups from egress-sidecar-macos)

- Port the DNS probes that `internet-proxy-locally` runs and
  `scripts/e2e-egress-sidecar.py` does not (`dns-rebinding`, `dns-mixed-answers`,
  `ptr-allowlist`): they need upstream's lab DNS fixture (egress-sidecar-macos
  design.md Decision 15).
- Capture the business and enterprise Copilot hosts with
  `scripts/e2e-copilot-egress.py`: `api.{business,enterprise}.githubcopilot.com`
  are admitted by analogy with the individual-plan capture (egress-sidecar-macos
  design.md Decision 5).
- `--copilot-host` GHE.com tenants in sidecar mode (rejected today): capture the
  tenant host set and add a tenant profile (egress-sidecar-macos).
- Sidecar egress for devcontainers. Sidecar launches leave `.devcontainer/`
  untouched; devcontainers stay firewall-only (egress-sidecar-macos).
- Per-sandbox denial attribution, smart GitHub repository access and upstream
  proxy authentication are on the ROADMAP ("Egress sidecar follow-ups",
  "Smart GitHub repository access in sidecar egress mode";
  egress-sidecar-macos).

## Egress sidecar on Linux (follow-ups from egress-sidecar-linux)

- Concurrent sessions sharing a local service on Docker Engine: firewall mode's
  `linux-bridge-socat` binds `<docker0 gateway>:<port>` per session, so a second
  concurrent firewall-mode or sidecar-mode session that uses the same Ollama or
  loopback `--agent-proxy` fails its bind check ("Cannot bind the … proxy to
  172.17.0.1:<port>"). The sidecar port itself is shared (egress-sidecar-linux).
- ufw and other host firewalls on Docker Engine are untested: a firewall that
  drops container traffic to the `docker0` gateway blocks the sidecar and the
  parent forwarder. The sandbox firewall reports the sidecar as unreachable with
  a hint naming ufw, but nothing is configured automatically
  (egress-sidecar-linux).
