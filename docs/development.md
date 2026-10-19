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
command catches syntax errors. `pytest -q` runs the unit tests without probing
Docker/Podman or launching containers. The Git and jj container integration
classes require an explicit opt-in (and may download their test images):

```bash
PROJECT_SANDBOX_CONTAINER_TESTS=1 uv run pytest -q tests/test_worktree.py tests/test_jj_workspace.py
```

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
`--no-build`, and `--keep`.

A separate suite covers `--python-uv`, checking inside the sandbox that the
agent user can actually run `uv` against the baked `/opt/venv`. It builds one
project with `python-preference = "only-managed"`, so uv downloads
its own interpreter during the build even if a suitable system Python is
installed. That interpreter must stay outside root's home or the agent cannot
read it. It synthesises its own Dockerfile, so
it needs a real container runtime and takes no `--base-image` / `--no-build`:

```bash
./scripts/e2e-python-uv.sh --runtime apple-container
```

Run `./scripts/run-e2e-tests.sh` to execute the complete E2E matrix available
on the host. This also includes Dockerfile-tamper checks, container timeout
teardown when a real runtime is selected, and availability-gated Ollama checks. Pass `--with-agent-proxy` only when you intend
to run the gateway-only network/credential isolation audit followed by two
billable Pi/OpenCode LLM requests.

The Node/npm smoke script creates a small Vite app, resolves and locks
`@playwright/test`, Vite, and `lodash-es` on the host, and builds the sandbox.
Each of two sessions runs a production build, checks its output, and runs the
page-rendering/click test against both system Chromium and Playwright's bundled
Chromium with the firewall enabled. Importing `lodash-es` exercises dependency
pre-bundling; the script asserts that Vite emitted `.vite` cache metadata and
JavaScript inside the writable/executable `node_modules` tmpfs. It also checks
host-directory isolation and fresh dependencies/cache state in the second
session. The first session asserts the default 2 GiB `/dev/shm`; the second
asserts an explicit `--shm-size 1g` override. It uses a Bash agent without AI
credentials.
Requires host npm, uv, build-time Internet access, and a running image-based
runtime:

```bash
./scripts/e2e-node-npm.sh --runtime apple-container --keep
./scripts/e2e-node-npm.sh --runtime docker
./scripts/run-e2e-tests.sh --only node-npm --runtime docker --keep
```

Workdirs live under `.project-sandbox/e2e/`. Failures retain the workdir for
debugging; `--keep` also retains successful runs and Playwright screenshots.

By default every suite that is available on the host runs. To iterate on a
single one, pass `--only SUITE`; `--list` prints the accepted suite names:

```bash
./scripts/run-e2e-tests.sh --list
./scripts/run-e2e-tests.sh --only git-workflow
```

Naming an opt-in suite with `--only` is itself the opt-in, so `--only
proxy-endpoints`, `--only agent-proxy`, `--only agent-proxy-isolation`, and `--only
internet-proxy-isolation` run without their corresponding `--with-*` flags
(the destructive Internet audit still requires its
`--blocked-url`, `--internet-proxy-dir`, and `--agentgateway-dir` arguments).
Availability gates still apply: a selected suite that needs a container
runtime, `jj`, or Ollama reports a skip when that dependency is missing.

The console output is one progress line per suite:

```
[1/12] smoke                      PASS (0s)
[4/12] jj-workflow                SKIP (jj not found on PATH)
[2/12] env-injection              FAIL (0s)  log: /tmp/project-sandbox-e2e-logs.BNS540/env-injection.log
```

Each suite's stdout and stderr go to `<suite>.log` in a per-run temp directory.
A failing suite's log is kept, its path is printed with the failure, and its
last 20 lines are echoed inline for immediate context. A passing suite's log is
deleted unless `--keep` is given, which retains every suite's log; when the run
leaves no logs behind, its temp directory is removed too.

Every run ends with a summary listing each suite it considered, in execution
order, as `PASS`, `FAIL`, or `SKIP` (with the skip reason), followed by a
`Ran N suite(s): ...` count. Skipped suites are not counted as run.

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

For a TLS-inspecting proxy, repeat `--ca-cert` for the public proxy CA
certificate chain:

```bash
uv run python scripts/e2e-internet-proxy-smoke.py \
  --runtime docker --ca-cert /path/to/corporate-root.pem
```

The full Internet-routing acceptance test is deliberately opt-in because it
stops and restarts both external services and makes real Agentgateway requests:

```bash
./scripts/run-e2e-tests.sh --runtime docker --with-internet-proxy \
  --blocked-url https://blocked.example.test/ \
  --internet-proxy-dir ../internet-proxy-locally \
  --agentgateway-dir ../agentgateway-locally \
  --ca-cert /path/to/corporate-root.pem
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
2. Runs Ruff, `ty check`, and pytest with `PROJECT_SANDBOX_CONTAINER_TESTS=1`
   to include the Git and jj sandbox container tests when their runtimes are
   available.
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

## Proxy endpoint verification

An optional end-to-end suite creates temporary HTTP/HTTPS gateway, proxy, and
origin fixtures, then launches normal headless Bash sessions through the CLI.
It uses a dummy gateway key and makes no billable requests. Existing services
are left running. Enable it explicitly:

```bash
# Replace this with a local non-loopback address reachable from the sandbox.
./scripts/run-e2e-tests.sh --only proxy-endpoints --runtime docker \
  --proxy-fixture-host 192.168.1.10
# Add to the full matrix with --with-proxy-endpoints --proxy-fixture-host IP.
```

Docker, Podman, and Apple container are accepted. The host needs OpenSSL and an
address the selected runtime can reach, including access to temporary listener
ports. The test binds only that address; the proxy accepts only its own fixture
origin and cannot relay arbitrary destinations. Images are built normally, so
build-time Internet access is required; `--no-build` is unsupported because each
run selects fresh ports and generates a temporary CA.

The suite checks remote HTTP and HTTPS, private CA installation, literal API
paths containing braces/brackets, authenticated direct gateway access from
Python/curl/Node, HTTP requests and HTTPS CONNECT through the Internet proxy,
and absence of gateway keys at that proxy. A separate live listener must receive
an administrative firewall denial, preventing closed-port false passes. An IPv4
fixture address also exercises an IPv4-mapped IPv6 gateway URL. To add native
IPv6 routing, pass `--proxy-fixture-ipv6-host` with a local unscoped IPv6 address
reachable from the runtime, or run the script directly:

```bash
uv run python scripts/e2e-proxy-endpoints.py --runtime docker \
  --fixture-host 192.168.1.10 --ipv6-host fd12:3456:789a::10 --keep
```

Temporary listeners always stop at exit. Successful runs remove their workdir
and test image; failures retain artifacts under `.project-sandbox/e2e/`, and
`--keep` retains successful artifacts too. This tests the generated image and
firewall with representative clients; real Pi/OpenCode requests, DNS-name
pinning, and IPv6-disabled fallback remain separate acceptance checks.

The controlled fixtures run without a container runtime or public Internet:

```bash
uv run pytest -q tests/test_proxy_endpoint.py tests/test_remote_proxy_cli.py tests/test_proxy_firewall.py tests/test_proxy_transport.py tests/test_e2e_proxy_endpoints.py
```

They cover parsing and dry-run combinations, executable sandbox resolution and
address/port rule selection, generated final TCP/TLS probes, private CA discovery,
redirect rejection, HTTP 407, expiry/name mismatch, and gateway key isolation.
The fixtures use temporary local servers and a temporary CA; synthetic DNS maps
remote fixture names to the server only within host-side transport tests. They
do not change host trust or install host firewall rules.

On 2026-10-14 the client transport checks passed with Python 3.11, curl 8.14.1
(OpenSSL 3.5.6), and Node v26.10.0 on Linux ARM64:

| Client path | HTTP proxy | HTTPS proxy | Trust/bypass tested |
| --- | --- | --- | --- |
| curl HTTP requests and CONNECT | Pass | Pass | Separate proxy CA and destination CA |
| Node built-in fetch | Pass | Pass | `NODE_USE_ENV_PROXY=1`, private CA, direct gateway via `NO_PROXY` |
| Python gateway discovery | Direct | Direct TLS | Ambient proxies ignored, private CA, no redirects |

Node's test uses `NODE_EXTRA_CA_CERTS` for an isolated fixture trust file; images
install the CA into OS trust and use `NODE_USE_SYSTEM_CA=1`. Proxy-aware Node
fetch requires `NODE_USE_ENV_PROXY=1` (or a client-specific proxy dispatcher).
This does not certify every bundled SDK: the image pins Node v26.11.1, and actual
Pi/OpenCode client execution still needs the runtime matrix below. Public-CA
live service checks were not run; normal system trust remains enabled and the
fixture tests confirm that an unknown private CA is rejected.

The generated isolation audit checks direct IP/TCP and UDP/DNS, unset proxy
variables, curl `--noproxy '*'`, cross-endpoint port attempts for distinct
admitted pairs, and either service stop order. Shared IPs necessarily share
already-authorized ports and are excluded from cross-port denial assertions.
Cross-port checks require an administrative rejection; a refused connection or
timeout is inconclusive and fails the audit rather than proving firewall isolation.
Run the existing checker twice on a host with controlled external services:

```bash
uv run python scripts/e2e-internet-proxy-isolation.py --runtime docker \
  --internet-proxy https://proxy.test:8443 --gateway https://models.test/v1 \
  --ca-cert /path/to/test-ca.pem --blocked-url https://denied.test/ \
  --internet-proxy-dir /path/to/proxy --agentgateway-dir /path/to/gateway \
  --stop-order proxy-first
# Repeat with --stop-order gateway-first, then with podman and apple-container.
```

This checker stops/restarts the named external services, so use dedicated test
instances and provide their control-command overrides where appropriate. The
normal CLI never manages external services. Check local Ollama separately with
the existing Ollama runtime acceptance suite, including a remote Internet proxy.

Docker, Podman, and Apple container were unavailable in the implementation
environment, which also lacked network-administration capabilities. Actual
firewall enforcement against bypassing/unsupported clients, IPv6 routing,
runtime forwarding, full Pi/OpenCode requests, and live service-stop independence
remain runtime acceptance checks. Unit tests execute the generated audit with
controlled successes/failures and ensure each failed probe makes it fail.

With `--python-uv` or this repository's custom `Dockerfile`, sandbox commands
use `/opt/venv` and managed Python
installations under `/opt/uv-python`. These paths stay inside the container,
so running `uv` on the host can update the host `.venv` independently. The
generated Codex configuration forwards both `UV_PROJECT_ENVIRONMENT` and
`UV_PYTHON_INSTALL_DIR` to its shell commands. Rebuild the sandbox after updating
project-sandbox to regenerate that configuration for an existing image.
