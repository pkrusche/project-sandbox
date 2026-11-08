# TODO - outstanding items for next release

## Egress sidecar on Linux (follow-ups from egress-sidecar-linux)

- Concurrent sessions sharing a local service on Docker Engine: firewall mode's
  `linux-bridge-socat` binds `<docker0 gateway>:<port>` per session, so a second
  concurrent firewall-mode or sidecar-mode session that uses the same Ollama or
  loopback `--agent-proxy` fails its bind check ("Cannot bind the … proxy to
  172.17.0.1:<port>"). The sidecar port itself is shared (egress-sidecar-linux).
