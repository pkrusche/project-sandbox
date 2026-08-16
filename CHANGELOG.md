# Changelog

Notable changes to `project-sandbox` are documented here. 

## [0.1.3]

### Added

- Local agent-proxy support for Pi, OpenCode, and interactive or headless Bash,
  including model discovery and automatic Pi/OpenCode proxy configuration.
- Gateway-only network policy for proxy sessions, with explicit domain opt-ins.
- End-to-end checks for proxy network isolation, injected credentials, Pi and
  OpenCode configuration, local Ollama, workflow integration, timeout cleanup,
  and generated Dockerfile integrity.
- A single host-side test runner and an end-to-end confirmation during releases.
- Unsupervised session records, live Markdown output, and stronger host locking.
- Rust formatting and linting components for Cargo projects.

### Changed

- Apple container forwarding for Ollama and agent proxy now consistently uses
  `host.docker.internal` and documents the required container-system DNS setup.
- Agent session observability, cleanup, and best-effort record handling are more
  robust.

## [0.1.2]

### Added

- Local Ollama support and a Pi agent integration.
- Separate agent configuration and credential handling.
- Cached Docker image builds based on generated build inputs.
- TestPyPI and PyPI publishing in the release workflow.
- OpenSpec telemetry opt-out inside generated environments.

### Changed

- Refined the interactive release workflow and version updates.

## [0.1.1]

### Added

- Support for Git dependencies in uv-based Python projects.

### Changed

- Made jj renderer tests independent of the installed jj version.

## [0.1.0]

Initial public release.

### Added

- Sandboxed Claude Code, Codex CLI, OpenCode, Pi, and Bash sessions using Apple
  container, Docker, Podman, or the portable test chroot.
- Generated devcontainer files, restricted network access, and selective agent
  credential forwarding.
- Git worktree and jj workspace workflows with headless execution support.
- Python/uv and Rust/Cargo project setup with pinned agent dependencies.
- Build caching, dry-run support, timeout teardown, end-to-end tests, and CI.
- Ruff, pytest, and release preflight checks.

[0.1.3]: https://github.com/pkrusche/project-sandbox/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/pkrusche/project-sandbox/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/pkrusche/project-sandbox/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pkrusche/project-sandbox/releases/tag/v0.1.0
