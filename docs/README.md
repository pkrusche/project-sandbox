# Documentation

See [Internet proxy](internet-proxy.md) for fail-closed routing through an externally managed filtering proxy.

This directory contains the detailed project-sandbox documentation.

## Contents

- [Usage guide](usage.md) - installation, quick start, custom Dockerfiles,
  devcontainer setup, unsupervised sessions, runtime selection, and dependency
  pin updates.
- [Generated files and runtime behavior](runtime.md) - end-to-end flow, file
  layout, generated image tags, OpenSpec, credential staging, and workspace
  masking.
- [Security model](security.md) - firewall behavior, OAuth token lifetime,
  threat model, troubleshooting, and limitations.
- [Development guide](development.md) - local setup, verification commands, test
  coverage, and end-to-end smoke tests.
- [Apple container host alias](apple-container-dns.md) - the shared
  `host.docker.internal` localhost DNS domain used by agent-proxy, Internet-proxy,
  and Ollama forwarding, including how to recover a stale alias.
- [References and related projects](references.md) - similar projects and
  alternate sandboxing approaches.
