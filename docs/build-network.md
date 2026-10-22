# Corporate network access during image builds

Use `--build-proxy-from-env` to configure downloads during image construction,
and `--build-ca-cert PATH` to trust a corporate CA before the first download:

```bash
export HTTP_PROXY=http://proxy.corp.example:3128
export HTTPS_PROXY=http://proxy.corp.example:3128
export NO_PROXY=localhost,127.0.0.1,.internal.example
project-sandbox --build-proxy-from-env --build-ca-cert /path/to/corporate-root.pem \
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

Only `http_proxy`, `https_proxy`, `all_proxy`, and `no_proxy`, and their uppercase
forms, are read. Lowercase takes precedence when present, including an empty
value; both cases are emitted with the same value. Unset values become empty.
At least one nonempty proxy URL must be set. URLs must be credential-free HTTP
or HTTPS endpoints. SOCKS proxies are unsupported. Apt uses the corresponding
HTTP/HTTPS variable, falling back to `ALL_PROXY`, or connects directly when
neither is set. Its bypasses use `no_proxy`.

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
project-sandbox --build-proxy-from-env --build-ca-cert /path/to/corporate-root.pem \
  --internet-proxy http://proxy.corp.example:3128 --runtime docker \
  --agent bash . python:3.12-slim
```

The existing `--ca-cert` installs trust after all dependency and project build
commands. It remains separate and requires `--internet-proxy` or `--agent-proxy`.
It also supplies trust for host-side HTTPS proxy checks; a build CA alone does
not change the host's trust store. See [runtime proxy configuration](internet-proxy.md).

## Builder connectivity and rebuilds

The proxy must be reachable from the image builder. Build proxy addresses are
used verbatim: `127.0.0.1` identifies the builder's own loopback, and no local
forwarder or host alias is automatically created. Use a host address accessible
from the builder, or configure the builder's host networking separately.

These flags configure commands inside image stages. Registry pulls and remote
`ADD` downloads are performed by the builder and need separate daemon/builder
proxy and certificate configuration. Configure the proxy's allowlist for all
package repositories and redirects used by the build.

Changed proxy values or certificate contents invalidate generated build inputs.
Removing the options removes their generated configuration and staged CA files
on the next render. Rebuild the image, including devcontainers, to apply
changes; `--no-build` continues using the existing image. `--dry-run` validates
and describes the setup without writing files or contacting the proxy.

## Squid end-to-end test

The opt-in test uses [internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally)
with Squid and TLS interception. Supply its checkout and a local non-loopback
host IP reachable from both the builder and runtime:

```bash
./scripts/run-e2e-tests.sh --only build-proxy --runtime docker \
  --internet-proxy-dir ../internet-proxy-locally \
  --proxy-fixture-host 192.168.1.20
```

Replace the example IP with your host's address. Docker and Apple `container`
are supported. Use `--with-build-proxy` instead of `--only build-proxy` to include
it in the full suite. Builds must be enabled. Run the standalone harness with:

```bash
uv run python scripts/e2e-build-proxy.py --runtime docker \
  --internet-proxy-dir ../internet-proxy-locally \
  --fixture-host 192.168.1.20 --keep
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
