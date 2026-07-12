# project-sandbox

`project-sandbox` runs Claude Code, Codex CLI, OpenCode, Pi, or a plain Bash shell
inside per-project Linux containers. On macOS, direct CLI runs default to Apple's
[`container`](https://github.com/apple/container) runtime, where each container
runs in its own VM. On Linux, direct CLI runs support Docker or Podman.

> ⚠️ Created with the help of AI. \
> 🚧 Experimental work in progress \
> ‼️ Use at your own risk.

## Main features

Many sandboxes exist - this is the one with a feature-set / configureable agency-boundary that I was comfortable with in the end:

* **Strong isolation** - on OSX with Apple Container VMs. Custom Dockerfile support (as long as it's based on Debian)
* **Agent config glue**: Forward host credentials / agent subscriptions into containers selectively, update settings to bypass permissions inside the container.
* **Devcontainer support**: Creates a matched devcontainer config for editor support (weaker isolation but integrated workflow).
* **Unsupervised job runs** - submit batch jobs.
* **Network access restrictions**: restrict to allowed domains, (somewhat) hardened firewall script.
* **git/jj integration**: Managed execution with worktrees / workspaces. No credentials to push inside containers.
* **pinned dependencies**: pre-install agents & extra tools into the image, manual upversioning.
* **Simple workflow** (in my view).
* **Minimal dependencies** (jinja2)

## Quick Start

Run directly from GitHub (if you trust the code):

```bash
uvx --from git+https://github.com/pkrusche/project-sandbox.git project-sandbox --help
```

From a checkout:

```bash
uv sync
uv run project-sandbox --help
```

Generate sandbox files for a project:

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

Preview every action without writing files or starting a runtime:

```bash
uv run project-sandbox --dry-run /absolute/path/to/repo python:3.12-slim
```

Start an agent in the sandbox:

```bash
uv run project-sandbox --agent codex /absolute/path/to/repo python:3.12-slim
```

Build on top of an existing project Dockerfile:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

## Documentation

- [Documentation index](docs/README.md)
- [Usage guide](docs/usage.md)
- [Generated files and runtime behavior](docs/runtime.md)
- [Security model](docs/security.md)
- [Development guide](docs/development.md)
- [References and related projects](docs/references.md)

## License

MIT. See [LICENSE](LICENSE).
