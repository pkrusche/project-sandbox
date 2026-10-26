# Documentation

This directory contains the detailed project-sandbox documentation.

## Contents

- [Usage guide](usage.md) - installation, quick start, custom Dockerfiles,
  devcontainer setup, unsupervised sessions, runtime selection, and dependency
  pin updates.
- [Host prerequisite checks](doctor.md) - `project-sandbox doctor` checks
  container runtimes, git/jj, and networking tools before a first run.
- [Headless browser testing](headless-browser-testing.md) - Playwright setup,
  agent-neutral task prompts, and reusable application instructions.
- [Generated files and runtime behavior](runtime.md) - end-to-end flow, file
  layout, generated image tags, OpenSpec, credential staging, and workspace
  masking.
- [proxied build networking](build-network.md) - early proxy and CA setup,
  build/runtime separation, and the Squid acceptance test.
- See [Internet proxy](internet-proxy.md) for routing traffic through an externally 
  managed filtering proxy.
- [Proxy endpoints](proxy-endpoints.md) - accepted local/remote URLs, DNS pinning,
  verified TLS, and independent gateway and Internet routing.
- [Security model](security.md) - firewall behavior, OAuth token lifetime,
  threat model, troubleshooting, and limitations.
- [Development guide](development.md) - local setup, verification commands, test
  coverage, and end-to-end smoke tests.
- [Apple container host alias](apple-container-dns.md) - the shared
  `host.docker.internal` localhost DNS domain used by agent-proxy, Internet-proxy,
  and Ollama forwarding, including how to recover a stale alias.
- [References and related projects](references.md) - similar projects and
  alternate sandboxing approaches.
