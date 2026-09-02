# Development Guide

## Local Setup

Use uv for the local environment:

```bash
uv sync
uv run project-sandbox --help
uv run python -m compileall src tests
uv run pytest -q
./scripts/check-ruff.sh
```

`uv sync` installs dependencies from `pyproject.toml` / `uv.lock`. The compile
command catches syntax errors. `pytest -q` runs the full test suite.
`scripts/check-ruff.sh` verifies that Python files have Ruff formatting applied
and contain no Ruff lint violations. For behavior previews, use
`uv run project-sandbox --dry-run ...`; dry-run must not write files or start
containers.

## Image build cache

`build_cache.py` fingerprints the generated build inputs and records the
fingerprint plus image tag in `.project-sandbox/.build-state.json`. `cli.py`
skips the build when the fingerprint matches and `container_cli.image_exists()`
confirms the image is present; auto-skip is limited to the default flow where the
build context equals the generated `.project-sandbox` dir. `dockerfile.render_dockerignore()` writes a scoped `Dockerfile.dockerignore`
(BuildKit's per-Dockerfile ignore convention) for the `--python-uv` flow only —
the one whole-project context whose Dockerfile we generate — so it doesn't tar
virtualenvs/caches to the daemon. It is skipped for user-supplied `--dockerfile`
builds (which may copy those paths) and when the project has its own root
`.dockerignore` (left authoritative). All of this degrades safely: any mismatch
or inconclusive check falls through to a normal build.

## Tests

Tests cover CLI surface, runtime selection, dry-run non-mutation, renderer
output, container `argv` construction, devcontainer JSON validity and symlinks,
gitignore helpers, image-build caching, and Python-native unsupervised-session
timeout handling.

A self-contained end-to-end smoke test creates a throwaway hello-world project,
runs the tool against it, and validates every generated artefact:

```bash
./scripts/e2e-test.sh                  # portable: devcontainer-only path
./scripts/e2e-test.sh --with-container # also exercises direct CLI container runs
```

The test prints the temp project path on success so the generated files can be
inspected.

Branch workflow end-to-end tests exercise real headless bash-agent runs against
throwaway git and jj repositories. They verify the finish actions that integrate
or leave agent work after the session:

```bash
./scripts/e2e-env-injection.sh
./scripts/e2e-git-workflow.sh
./scripts/e2e-jj-workflow.sh
```

All three scripts default to `--runtime chroot` on Linux and accept
`--runtime chroot|auto|apple-container|docker|podman`, `--base-image IMAGE`,
`--no-build`, and `--keep`. Run `./scripts/run-e2e-tests.sh` to execute the
complete E2E matrix available on the host. This also includes Dockerfile-tamper
checks, container timeout teardown when a real runtime is selected, and
availability-gated Ollama checks. Pass `--with-agent-proxy` only when you intend
to run the gateway-only network/credential isolation audit followed by two
billable Pi/OpenCode LLM requests.

When a real container runtime is selected, the aggregate suite also runs a
non-destructive Internet-proxy smoke test. If no listener is available at
`http://127.0.0.1:18080`, it prints a `SKIP` note without creating a project,
building an image, or starting a container. When the listener is present, it
verifies that allowlisted HTTPS succeeds through the proxy and that
`curl --noproxy '*'` cannot bypass the sandbox firewall. If Agentgateway is
also listening on `http://127.0.0.1:4000/v1` and its key is available, the same
no-credential-forwarding sandbox also verifies authenticated Agent proxy access
and the absence of forwarded host credential files. Otherwise it prints a skip
note for that combined scenario. Run it directly with:

```bash
uv run python scripts/e2e-internet-proxy-smoke.py --runtime apple-container
```

The full Internet-routing acceptance test is deliberately opt-in because it
stops and restarts both external services and makes real Agentgateway requests:

```bash
./scripts/run-e2e-tests.sh --runtime docker --with-internet-proxy \
  --blocked-url https://blocked.example.test/ \
  --internet-proxy-dir ../internet-proxy-locally \
  --agentgateway-dir ../agentgateway-locally
```

Use a denied public-domain fixture configured by the proxy, not the placeholder
above. The script verifies the proxy-policy denial separately
from firewall failures; direct curl, unset-proxy, raw TCP, UDP, and DNS bypasses;
a real AI completion; cross-service routing; independent failure; fail-closed
proxy loss; and stable endpoint recovery through `uv run ipl restart`. The
Internet proxy control defaults to `uv run ipl {action}`, while Agentgateway
continues to use `./run.py {action}`. Both can be overridden on the standalone
script when an installation uses a different lifecycle command; by default we
assume the internetproxy-locally and agentgateway-locally examples.

Use the unified host-side entry point for full local verification:

```bash
./scripts/run-host-tests.sh
./scripts/run-host-tests.sh --runtime docker
./scripts/run-host-tests.sh --runtime apple-container --with-agent-proxy
```

It runs `uv sync --locked`, compile checks, Ruff, pytest, and the complete E2E
aggregator in that order. On Linux the default `chroot` runtime keeps the run
portable; select a real runtime to include container-only checks.

## Releasing

Before creating a release, run the comprehensive host test suite against every
supported real container runtime available to you:

```bash
./scripts/run-host-tests.sh --runtime apple-container --with-agent-proxy
./scripts/run-host-tests.sh --runtime docker --with-agent-proxy
./scripts/run-host-tests.sh --runtime podman --with-agent-proxy
```

Run the Apple container command on macOS and the Docker and Podman commands on
hosts where those runtimes are installed. The local agent gateway and its key
must be configured before running this matrix. `--with-agent-proxy` includes the
non-billable gateway isolation audit followed by two billable Pi/OpenCode LLM
requests. Confirm that every applicable runtime passes before starting the
release workflow; record any unavailable runtime in the release notes or pull
request verification summary.

`scripts/make-release.sh` drives the full release workflow interactively:

1. Verifies the working copy is clean (jj-aware).
2. Runs Ruff and pytest checks.
3. Prompts to confirm or bump the version in `pyproject.toml`. If changed, the
   script describes the jj change and opens a new empty change, or creates a Git
   commit, then exits so you can push the bump and re-run.
4. Creates a GitHub release and tag via the `gh` CLI (`gh` must be installed and
   authenticated).

5. Builds the wheel and source distribution with `uv build`.
6. Publishes to `test.pypi.org` after explicit confirmation and a hidden token prompt.
7. Publishes to `pypi.org` after explicit confirmation and a separate hidden token prompt.

Progress is tracked in `.release-status/` (git-ignored).  Re-running the script
after a failure resumes from the last incomplete step.  Delete `.release-status/`
to start a fresh release cycle.
