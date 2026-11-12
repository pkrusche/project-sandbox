# Documentation

**Using project-sandbox**

- [Usage guide](usage.md): install and `doctor`, images and flows, agents and
  credentials, headless and branch sessions, devcontainers.
- [Network access](networking.md): egress modes, `--internet-proxy`, endpoint
  rules, CA certificates, build-time proxies.
- [Agent proxy](agent-proxy.md): routing model traffic through a gateway.
- [Egress sidecar](egress-sidecar.md): `--egress sidecar` and
  `project-sandbox-egress`.
- [VCS access](vcs-access.md): read-only or hidden Git/jj metadata.
- [Headless browser testing](headless-browser-testing.md): Playwright in the
  sandbox.
- [Apple container host alias](apple-container-dns.md): the one-time
  `host.docker.internal` setup and how to recover a stale alias.

**How it works**

- [Generated files and runtime behavior](runtime.md): flow, file layout,
  masking, concurrent sessions.
- [Security model](security.md): isolation, credentials, firewall, threat
  model, limitations.

**Contributing**

- [Development guide](development.md): setup, tests, e2e suites, pin updates,
  releases.
- [Security verification](security-verification.md): the live security suite.
- [References and related projects](references.md).
