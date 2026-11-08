# TODO - outstanding items for next release

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
