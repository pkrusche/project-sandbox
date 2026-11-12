# project-sandbox

[![PyPI](https://img.shields.io/pypi/v/project-sandbox.svg)](https://pypi.org/project/project-sandbox/)

Run Claude Code, Codex CLI, OpenCode, Pi, GitHub Copilot CLI or a plain shell
inside a per-project Linux container, supervised or headless. On macOS it uses
Apple's [`container`](https://github.com/apple/container) runtime, where each
container is its own VM; on Linux, Docker or rootless Podman.

> ⚠️ Created with the help of AI. 🚧 Experimental. ‼️ Use at your own risk.

- **Isolation:** only the project and the files an agent needs are mounted.
- **Credentials:** each agent gets only its own forwarded login, staged per
  session.
- **Network:** default-deny firewall with allowlisted domains, an external
  proxy, or a managed hostname-filtering sidecar.
- **Git and jj:** optional per-branch worktrees or workspaces, captured on the
  host after the session; agents can't push.
- **Projects:** Python/uv, Rust/Cargo, Node/npm or your own Dockerfile, with
  pinned agent and tool versions, plus a matching devcontainer.

## Quick start

```bash
uv tool install project-sandbox
project-sandbox doctor                                         # check prerequisites
project-sandbox --agent claude /path/to/repo python:3.12-slim  # start a session
```

Add `--dry-run` to see what would happen without writing files or starting
anything.

## Documentation

Start with the [usage guide](https://github.com/pkrusche/project-sandbox/blob/main/docs/usage.md);
the [documentation index](https://github.com/pkrusche/project-sandbox/blob/main/docs/README.md)
lists the rest, including the [security model](https://github.com/pkrusche/project-sandbox/blob/main/docs/security.md).
See also the [changelog](CHANGELOG.md) and [roadmap](ROADMAP.md).

## License

MIT. See [LICENSE](LICENSE).
