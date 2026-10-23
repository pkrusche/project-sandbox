# Corporate network access during image builds

Use `--build-proxy URL` to configure downloads during image construction,
and `--build-ca-cert PATH` to trust a corporate CA before the first download:

```bash
project-sandbox --build-proxy http://proxy.corp.example:3128 \
  --build-ca-cert /path/to/corporate-root.pem \
  --runtime docker --build-only . python:3.12-slim
```

These options work with base images, `--dockerfile`, `--python-uv`, `--rust-cargo`,
and `--node-npm`, and are included in both generated sandbox and devcontainer
Dockerfiles. They require an image-based runtime; chroot is unsupported.
`--build-ca-cert` also works independently, for example with an internal HTTPS
package mirror. Each public PEM file may contain one CA or a bundle of CAs. Repeat the option
for multiple files. Each certificate is validated and installed separately.
Private keys, leaf certificates, and malformed certificate files are rejected.

## Build settings and runtime permissions

Build proxy settings configure the environment and apt before the build's
commands run. For supplied multi-stage Dockerfiles, setup covers executable
Debian/Ubuntu stages, including a stage named `prefix`. Scratch stages,
recognized non-Debian images, and stages only used as copy sources are left
alone. Existing Dockerfile instructions can override the injected settings.
The bootstrap explicitly uses `/bin/sh`, so an inherited custom `SHELL` does
not change how certificate and apt proxy setup runs.

The proxy URL is set as `http_proxy` and `https_proxy`, in lowercase and
uppercase, and as apt's HTTP and HTTPS proxy. `no_proxy` is fixed to
`localhost,127.0.0.1,::1`. Host proxy variables are not read. The URL must be a
credential-free HTTP or HTTPS endpoint without a path. SOCKS proxies are unsupported.

Proxy environment values and the generated apt proxy file are cleared after
all build commands, before the agent starts. They can still appear in generated
Dockerfiles, build logs, and intermediate image layers. Corporate CA trust
persists in the image. System roots are retained; Node, Python requests, and uv
are configured to use the extended system bundle. Applications with their own
trust stores may need additional configuration. Certificate verification stays
enabled, including the initial apt request when `ca-certificates` is absent.

**Build flags never open the runtime firewall.** To use the proxy at runtime,
add `--internet-proxy` explicitly:

```bash
project-sandbox --build-proxy http://proxy.corp.example:3128 \
  --build-ca-cert /path/to/corporate-root.pem \
  --internet-proxy http://proxy.corp.example:3128 --runtime docker \
  --agent bash . python:3.12-slim
```

The existing `--ca-cert` installs trust after all dependency and project build
commands. It remains separate and requires `--internet-proxy` or `--agent-proxy`.
It also supplies trust for host-side HTTPS proxy checks; a build CA alone does
not change the host's trust store. See [runtime proxy configuration](internet-proxy.md).

## Builder connectivity and rebuilds

With Docker and Podman, the proxy URL is used verbatim and must be reachable
from the image builder: `127.0.0.1` identifies the builder's own loopback. Use
a host address accessible from the builder, or configure the builder's host
networking separately.

### Apple container: host-loopback forwarder

Apple container's bridge network cannot reach a proxy that is only routable
through the host's VPN. For Apple container builds, project-sandbox therefore
starts a Python standard-library TCP forwarder on the host:

```text
builder -> http://203.0.113.113:PORT -> host 127.0.0.1:PORT (forwarder) -> proxy
```

The forwarder resolves and connects to the proxy from the host, so VPN routes
and DNS apply. It relies on the
[Apple container localhost alias](apple-container-dns.md) that maps
`203.0.113.113` to the host loopback; the one-time setup command is printed
when forwarding is used. The builder always speaks plain HTTP to the
forwarder. For an `https://` proxy, the forwarder opens TLS to the proxy from
the host and verifies it with system trust plus `--build-ca-cert` files.

`PORT` is derived from the proxy URL (range 40000–48999), so the rendered
Dockerfile and the layer cache stay stable across runs. If that port is busy,
a random free port is used with a warning, which invalidates the build cache
for that run. The forwarder only listens on `127.0.0.1`, starts just before
the image build, and stops when the build finishes, before any sandbox starts.
Other local processes can use it while it runs. Cached-image runs start no
forwarder. If the proxy cannot be reached or its TLS certificate fails
verification, the forwarder prints one warning and closes the builder's
connection.

The forwarded URL is written to both the sandbox and devcontainer Dockerfiles.
Builds outside project-sandbox, such as a devcontainer rebuild, have no
forwarder running and fail to reach the proxy. Rebuild with project-sandbox,
or render the devcontainer for a runtime that can reach the proxy directly.

These flags configure commands inside image stages. Registry pulls and remote
`ADD` downloads are performed by the builder and need separate daemon/builder
proxy and certificate configuration. Configure the proxy's allowlist for all
package repositories and redirects used by the build.

Changed proxy URLs or certificate contents invalidate generated build inputs.
Removing the options removes their generated configuration and staged CA files
on the next render. Rebuild the image, including devcontainers, to apply
changes; `--no-build` continues using the existing image. `--dry-run` validates
and describes the setup, including the Apple forwarder port, without writing
files, listening, or contacting the proxy.

## Squid end-to-end test

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
are supported; with Apple `container`, the builds exercise the host-loopback forwarder. Use `--with-build-proxy` instead of `--only build-proxy` to include
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
requests in Squid logs. Runtime probes verify proxy cleanup, retained CA trust,
blocked access without `--internet-proxy`, and successful access with that flag
while direct bypass remains blocked. It needs no LLM credentials or API calls.
Setup failures are failures, not skips. Logs and structured results are retained
on failure; `--keep` also retains successful artifacts and tagged test images.
