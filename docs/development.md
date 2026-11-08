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

The repository TUI's `:test` command runs pytest in parallel with
`-n auto --dist loadfile`. Keep tests from each file on the same worker because
the relay session tests share a fixed host port.

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

All three scripts default to `--runtime auto` (Apple `container` on macOS,
otherwise Docker, then Podman) and accept
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

Before the first suite, the runner stops sandbox containers left running by an
earlier sandbox CLI that was killed (for example with SIGKILL) before it could
tear down. Only containers whose session record is `orphaned` (the recorded CLI
PID no longer exists) are stopped; sandboxes still in use are left alone.

Suites tag a fresh image per temporary project, and interrupted runs can leave
containers and forwarders behind. Sweep them afterwards with:

```bash
uv run python scripts/e2e-cleanup.py --dry-run   # list what would be removed
uv run python scripts/e2e-cleanup.py             # every runtime found on PATH
uv run python scripts/e2e-cleanup.py --runtime docker
```

It removes sandbox containers running an e2e image, the IPL Squid fixture
started by the build-proxy suite (recognised by its config mount in a
`project-sandbox-build-proxy.*` temp directory, so a real IPL proxy is never
touched), shared local-service forwarders (`project_sandbox.egress_relay
--lease` processes with a `local-forward` state directory) that no session
leases any more, and images whose tags the suites generate. A forwarder that a
session still leases, and images of real projects, are left alone. It refuses
to run while an e2e script is active unless `--force` is given.

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

`--all-tests` runs every suite, including all opt-in ones. It implies
`--with-agent-proxy`, `--with-internet-proxy`, `--with-proxy-endpoints`,
`--with-build-proxy`, `--with-copilot`, `--with-copilot-egress`, and
`--with-egress-sidecar`, and exits early (64) unless `--internet-proxy-dir` and
`--agentgateway-dir` are supplied (and `--no-build` is not). These suites are
billable and destructive:

```bash
./scripts/run-e2e-tests.sh --all-tests --runtime docker \
  --internet-proxy-dir DIR --agentgateway-dir DIR
```

Two suites cover [`--egress sidecar`](egress-sidecar.md):

- `egress-sidecar` (`scripts/e2e-egress-sidecar.py`, not billable): real
  sessions on the runtime's global sidecar, which the sandbox reaches at its
  published endpoint through local-service forwarding. Its checks:
  - `policy`: probes graded from inside the sandbox and from the sidecar log,
    direct egress and host ports that are not local services, the session-end
    denial count, and the endpoint answering on host loopback only.
  - `shared`: two concurrent sandboxes on one sidecar, reuse across launches,
    a live `allow add`, and `down`.
  - `parent`: chaining through an intercepting Squid fixture. On Apple
    `container` it also checks that the sidecar-owned relay survives the first
    session's hung-up process group.
  - `local`: a fake agent gateway that listens only on the host's `127.0.0.1`,
    reached through local-service forwarding rather than the sidecar.

  It needs network access and image builds, uses a temporary
  `XDG_CONFIG_HOME`/`XDG_STATE_HOME`, and refuses to start while a global
  sidecar exists (`--replace-existing` overrides that). `--port` changes the
  sidecar's published port (default 3129).
- `copilot-egress` (`scripts/e2e-copilot-egress.py`, billable): captures the
  Copilot CLI's hosts and paths through an intercepting fixture, verifies the
  Copilot-only profile, and checks that GitHub repository access stays blocked.
  Its `sidecar` phase then runs a Copilot session and the same block probes
  through the real global sidecar with `[egress.copilot] enabled = true`,
  graded on the sidecar's log. That phase uses a temporary
  `XDG_CONFIG_HOME`/`XDG_STATE_HOME` (linking your `containers` and `gh`
  config) and refuses to start while a global sidecar exists
  (`--replace-existing` overrides that). It writes a report to
  `--copilot-egress-report-dir`.

On Podman, leave `DOCKER_HOST` unset (or pointed at Docker) for the
`egress-sidecar` suite. The parent fixture runs on Docker, and with
`DOCKER_HOST` set to the Podman socket, `docker build` uses a buildx
`docker-container` builder that does not load the image, so `parent` fails
with a pull error.

The Internet isolation audit expects the proxy to deny `--blocked-url`, which
defaults to `https://example.com`; pass it only to check a different URL.

Naming an opt-in suite with `--only` is itself the opt-in, so `--only
proxy-endpoints`, `--only agent-proxy`, `--only agent-proxy-isolation`, `--only
copilot`, `--only copilot-egress`, `--only egress-sidecar`, and `--only
internet-proxy-isolation` run without their corresponding `--with-*` flags
(the destructive Internet audit still requires its `--internet-proxy-dir` and
`--agentgateway-dir` arguments).
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
  --runtime docker --ca-cert /path/to/extra-roots.pem
```

The full Internet-routing acceptance test is deliberately opt-in because it
stops and restarts both external services and makes real Agentgateway requests:

```bash
./scripts/run-e2e-tests.sh --runtime docker --with-internet-proxy \
  --internet-proxy-dir ../internet-proxy-locally \
  --agentgateway-dir ../agentgateway-locally \
  --ca-cert /path/to/extra-roots.pem
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

### GitHub Copilot suite

The `copilot` suite (`scripts/e2e-copilot.py`) runs real GitHub Copilot CLI
sessions, so it is opt-in and **billable**: each check except the bash half of
`isolation` sends Copilot requests that count against the account's premium
request allowance. It runs only with `--with-copilot`, `--only copilot`, or
`--all-tests`; a default run skips it without any token lookup.

Prerequisites: a Copilot subscription, a real container runtime, and a token the
CLI can resolve, either a fine-grained PAT with only "Copilot Requests" in pass
(`pass insert copilot-github-token`, or `copilot-github-token/<tenant>.ghe.com`)
or a `gh auth login --hostname <host>`.

| Check | Account | Verifies |
|---|---|---|
| `headless-ok` | personal | a trivial prompt exits 0, the JSONL parses, the transcript renders |
| `headless-fail` | personal | an invalid `--model` gives a nonzero CLI exit; prints the raw exit and final record |
| `trusted-folder` | personal | no trust prompt/error in a `-p` run; `config.json` lists `/workspace` |
| `isolation` | personal | Copilot API reachable, `example.com` blocked, no built-in MCP tools; a bash session has no token mount or env |
| `tenant` | GHE.com tenant | tenant token resolution and a headless run through the pinned tenant hosts; `copilot-api.<tenant>` reachable |

```bash
# Personal account (tenant check reported as skipped)
./scripts/run-e2e-tests.sh --only copilot --runtime docker
uv run python scripts/e2e-copilot.py --runtime docker

# Also exercise a GHE.com data-residency tenant
./scripts/run-e2e-tests.sh --only copilot --runtime docker --copilot-host acme.ghe.com
uv run python scripts/e2e-copilot.py --runtime docker --copilot-host acme.ghe.com

# One check, keeping redacted captures
uv run python scripts/e2e-copilot.py --check headless-fail --capture-dir /tmp/copilot-captures
```

`--capture-dir DIR` writes `<check>.jsonl` (the session's JSON records) and
`<check>.exit.json` (exit code, record count, final record) per check, with the
token replaced by `[REDACTED]`; `--copilot-capture-dir` forwards it from the
runner. Copilot's JSONL schema and failed-run exit codes are undocumented, so
these captures are the source for `tests/fixtures` transcript fixtures and for
tightening the renderer in `transcript.py` and the exit reclassification in
`observability.copilot_json_stream_failure`. `--output-tail N` controls how many
redacted lines of CLI output a failing check prints (default 40).

Use the unified host-side entry point for full local verification:

```bash
./scripts/run-host-tests.sh
./scripts/run-host-tests.sh --runtime docker
./scripts/run-host-tests.sh --runtime apple-container --with-agent-proxy
```

It runs `uv sync --locked`, compile checks, Ruff, pytest, and the complete E2E
aggregator in that order. The default `auto` runtime picks a real container
runtime so container-only checks run; pass `--runtime chroot` on Linux for a
host-only run without Docker or Podman.

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
  --proxy-fixture-host 192.168.1.10 --proxy-fixture-hostname 192-168-1-10.sslip.io
# Add to the full matrix with --with-proxy-endpoints --proxy-fixture-host IP.
```

HTTPS endpoints must be named by DNS, so the HTTPS scenario runs only with
`--proxy-fixture-hostname` (`--fixture-hostname` for the standalone script): a
name that resolves to the fixture IP both on the host and inside the sandbox.
A wildcard-DNS service such as sslip.io works if your resolver allows public
names to answer with private addresses; a local DNS or `/etc/hosts` entry that
the runtime also sees works too. Without it the HTTPS scenario is skipped.

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
IPv6 routing (over HTTP), pass `--proxy-fixture-ipv6-host` with a local unscoped
IPv6 address reachable from the runtime, or run the script directly:

```bash
uv run python scripts/e2e-proxy-endpoints.py --runtime docker \
  --fixture-host 192.168.1.10 --fixture-hostname 192-168-1-10.sslip.io \
  --ipv6-host fd12:3456:789a::10 --keep
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
instances and provide their control-command overrides where appropriate. If the
Internet proxy listener is down at start, the checker runs its control command
with each `--internet-proxy-start` action (default `setup,up`, which builds a
missing image first) and with `down` when finished; a running proxy is left
running. The
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

## Squid build-network verification

The opt-in test uses [internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally)
with Squid and TLS interception. Supply its checkout:

```bash
./scripts/run-e2e-tests.sh --only build-proxy --runtime docker \
  --internet-proxy-dir ../internet-proxy-locally
```

The fixture IP defaults to the host’s outbound IPv4 source address on macOS
and Linux. Detection sends no packets. The selected address is printed; it must
be reachable from both the builder and runtime. On VPN or multi-interface hosts,
override it with `--proxy-fixture-host IP` (`--fixture-host IP` for the standalone
harness). Detection failures require an explicit override. Docker and Apple `container`
are supported; with Apple `container`, the builds exercise the host-loopback forwarder.
Apple `container` forwards published ports for loopback traffic only, so there the
Squid fixture is published on `127.0.0.1` for its health check and the builds and
sandbox reach it at its container address. Rootless Podman is accepted too. IPL
has no Podman backend, so the Squid fixture runs on Docker (which must also be
installed). Use `--with-build-proxy` instead of `--only build-proxy` to include
it in the full suite. Builds must be enabled. Run the standalone harness with:

```bash
uv run python scripts/e2e-build-proxy.py --runtime docker \
  --internet-proxy-dir ../internet-proxy-locally \
  --keep
```

The harness copies the proxy sources into a temporary workspace, extends only
that workspace's build-domain allowlist, chooses a separate endpoint, and runs
`ipl --engine squid setup --tls-interception`, `up --tls-interception`, and
`ca export`. It neither copies the caller's signing key nor controls existing
proxy instances. Squid is stopped on completion, including failure.

The test makes real, uncached HTTPS apt, curl, npm, and uv requests, checks
specific missing-CA and unreachable-proxy failures, and verifies decrypted
requests in Squid logs. Runtime probes use separate images with blocked and
allowed firewall policies to verify absence of build proxy settings, retained
CA trust, blocked access without `--internet-proxy`, and successful access with it
while direct bypass remains blocked. It needs no LLM credentials or API calls.
Setup failures are failures, not skips. Logs and structured results are retained
on failure; `--keep` also retains successful artifacts and tagged test images.

## Egress sidecar verification

The opt-in `egress-sidecar` suite (`scripts/e2e-egress-sidecar.py`) runs real
`--egress sidecar` sessions (agent `bash`, no credentials, not billable) against
the runtime's global sidecar. It grades every probe twice: from inside the
sandbox, and in the sidecar's access log. It needs network access, a container
runtime and image builds. All of its state lives in a temporary
`XDG_CONFIG_HOME`/`XDG_STATE_HOME`. The suite refuses to start while a global
sidecar exists on that runtime, unless you pass `--replace-existing`.

```bash
./scripts/run-e2e-tests.sh --only egress-sidecar --with-egress-sidecar --runtime docker
# One check, keeping the test projects and logs
uv run python scripts/e2e-egress-sidecar.py --runtime podman --checks policy --keep
```

| Check | Verifies |
|---|---|
| `policy` | the probe set of design.md Decision 15 in direct mode: allowed splice, unlisted host, IP literals, ports, metadata by name, SNI mismatch, raw tunnels, direct egress, DNS, GitHub repository access and concurrency; also the session-end denial count, session-input cleanup, and the endpoint answering on its one published address only (host loopback; on Docker Engine the default bridge gateway) |
| `intercept` | with the Copilot profile on, the intercepted `api.github.com`: CA trust, an allowed path reaching GitHub, labelled 403 pages, a foreign or missing SNI, and raw tunnels |
| `shared` | two concurrent sandboxes on one sidecar, reuse across launches, a live `allow add`, and `down` cutting both off |
| `parent` | chaining through an intercepting fixture parent proxy; on Apple `container`, the host relay surviving a hung-up session; on Docker Engine and rootless Podman, a parent listening only on host loopback, refused from the sandbox, and on Docker Engine the parent forwarder (live, in `status`, closing another container on the default bridge without the parent seeing it) |
| `local` | a service listening only on host loopback, reached through local-service forwarding rather than the sidecar |

A CONNECT denial passes only if the sidecar sends zero bytes and then closes or
resets the connection. A reply, a timeout or an unreachable sidecar fails it.

Results (egress-sidecar-macos task 9.1, 2026-11-08, macOS):

| Runtime | Result |
|---|---|
| Apple `container` CLI 1.7.1 | 88/88 passed |
| Docker Desktop 4.98.0 (Engine 29.9.2) | 87/87 passed |
| Podman machine, rootless (client 6.0.0, server 6.0.1) | 87/87 passed |

Apple `container` has one extra check, `parent:relay-survives-first-session`,
because only Apple `container` reaches the parent proxy through the host relay.

Results (egress-sidecar-linux task 5.1, 2026-11-08, Debian 13, kernel 6.12):

```bash
uv run python scripts/e2e-egress-sidecar.py --runtime docker
uv run python scripts/e2e-egress-sidecar.py --runtime podman
```

| Runtime | Endpoint | Result |
|---|---|---|
| Docker Engine 29.9.2 (rootful) | `172.17.0.1:3129` | 94/94 passed |
| Rootless Podman 5.4.2 with pasta | `127.0.0.1:3129` | 89/89 passed |

The Linux runs add `parent:loopback-only` (the fixture parent answers on
`127.0.0.1` only) and `parent:sandbox-refused:host.docker.internal:<parent
port>`. Docker Engine also checks `parent:forwarder-live`,
`parent:forwarder-in-status`, `parent:forwarder-closes-other-peers` (a container
on the default bridge gets a close and `relay.log` records `refused peer`),
`parent:never-saw-other-peer` and `parent:sandbox-refused:172.17.0.1:<forwarder
port>`. `policy:endpoint-loopback-only` becomes `policy:endpoint-gateway-only`
there: the endpoint answers on the gateway and not on `127.0.0.1` or any other
host address. The parent fixture runs on Docker in both runs. ufw was not
installed on the test host.

The Copilot profile through the real sidecar (egress-sidecar-macos task 9.2,
2026-11-08, Apple `container` CLI 1.7.1, individual plan):

```bash
uv run python scripts/e2e-copilot-egress.py --runtime apple-container --phases sidecar
```

- `sidecar`: passed. The Copilot session exited 0 with the expected reply.
  Copilot traffic went to `api.individual.githubcopilot.com`. On the
  intercepted `api.github.com`, `/copilot_internal/user` (200) and
  `/copilot_internal/managed_settings` (404, from GitHub) went through. The
  only denials were the expected ones: the CLI's update check
  `/repos/github/copilot-cli/releases` (`path-not-allowed`) and
  `telemetry.individual.githubcopilot.com` (`host-not-allowed`).
- `sidecar-block`: passed. Every GitHub repository probe was denied with the
  profile on. CONNECTs to `github.com`, `raw.githubusercontent.com`,
  `codeload.github.com` and `gist.github.com` were reset (`host-not-allowed`).
  `api.github.com` `/repos/…` and `/gists/public` got the sidecar's labelled
  403 (`path-not-allowed`), and `git ls-remote` exited 128.
