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
for multiple files. Each certificate is validated and installed separately,
but all of them are copied into a stage with a single `COPY`, so large bundles
do not add image layers. Private keys, leaf certificates, and malformed
certificate files are rejected.

## Build settings and runtime permissions

The proxy is passed to the image build as the builder's predefined proxy
build-args (`--build-arg http_proxy=…`, plus `https_proxy` and `no_proxy`, in
lowercase and uppercase). They are deliberately not declared with `ARG`. Every
`RUN` instruction sees them as environment variables, but they never appear in
the generated Dockerfiles, the image configuration, or its history. BuildKit
also excludes them from the layer cache key, so a changed proxy URL or
forwarder port reuses existing layers instead of rebuilding.

`no_proxy` defaults to `localhost,127.0.0.1,::1`. Add entries with
`--build-no-proxy HOSTS`. The option is repeatable and takes comma-separated
host names, `.domain` suffixes (optionally with `:port`), IP addresses, or CIDR
networks. It requires `--build-proxy`, and `*` is rejected. Host proxy variables
are not read. The URL must be a credential-free HTTP or HTTPS endpoint without
a path. SOCKS proxies are unsupported.

apt reads `http_proxy`, `https_proxy`, and `no_proxy` from the environment, so
no apt proxy file is written. Commands that clear the environment, such as
`sudo apt-get` under the default `env_reset`, do not see the proxy. Run them as
root, or pass the variables explicitly (`sudo -E`).

Because nothing is persisted, there is no proxy cleanup step: the agent starts
without proxy settings. The values can still appear in build logs and in the
builder's process arguments. Build CA setup runs early in executable
Debian/Ubuntu stages, including a stage named `prefix`. A stage qualifies when
its base image is a known Debian/Ubuntu image (`debian`, `ubuntu`, `python`,
`node`, `golang`, `rust`, and similar official images, or a Debian/Ubuntu
release name in the reference) or when the stage or its parent stages run
`apt-get`, `apt`, or `dpkg`. Non-Debian images, unresolvable `FROM ${ARG}`
bases, and unrecognized images are left alone with a warning, because the
configured bundle path exists only on Debian/Ubuntu. Scratch stages and stages
only used as copy sources are skipped silently.

The setup never changes a stage's user. It is placed after a `USER root` that
precedes the stage's first `RUN`, otherwise directly after `FROM`. A stage
whose inherited user is not root prints a notice and keeps that user; the CA
is then not installed there. The bootstrap explicitly uses `/bin/sh`, so an
inherited custom `SHELL` does not change how certificate setup runs. Corporate CA trust persists in the
image. System roots are retained; Node, Python requests, and uv
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

`PORT` is preferably derived from the proxy URL (range 40000–48999). The port
only reaches the builder as a build-arg, so if that port is busy, a random free
port is used with a notice and the Dockerfiles and the layer cache are unaffected.
Concurrent runs on the same project therefore never rewrite each other's
Dockerfile. The listener is bound without `SO_REUSEADDR`, so it never shares
a port with another listener, including a wildcard (`*:PORT`) listener on
macOS. A port still in `TIME_WAIT` from a recent run also falls back.

The forwarder only listens on `127.0.0.1`. It is created after the
cache check, inside the per-project build lock, and only when an image is
actually built. It stops when the build finishes, before any sandbox starts.
Cached-image and `--no-build` runs start no forwarder. Other local processes
can use it while it runs. Each connection is relayed with bounded,
non-blocking buffers, so large simultaneous uploads and downloads cannot
deadlock. A builder's half-close is honoured, and the proxy's reply is still
delivered, also for `https://` proxies. If the proxy cannot be reached or its
TLS certificate fails verification, the forwarder prints one warning and closes
the builder's connection. Hosts listed with `--build-no-proxy` are contacted
directly by the builder, not through the host, so VPN-only hosts must stay
proxied.

The generated `devcontainer.json` does not contain the proxy. It lies inside
whole-project build contexts, so values written to it would be copied into
image layers by `COPY . .` and would change their cache key. Devcontainer
tooling that builds outside project-sandbox needs its own builder proxy
configuration, for example the `proxies` section of `~/.docker/config.json`,
which Docker passes to builds as the same predefined build-args.

These flags configure commands inside image stages. Registry pulls and remote
`ADD` downloads are performed by the builder and need separate daemon/builder
proxy and certificate configuration. Configure the proxy's allowlist for all
package repositories and redirects used by the build.

Changed certificate contents invalidate generated build inputs; changed proxy
settings do not, because they are not build inputs. Removing the options removes
their generated configuration and staged CA files on the next render. Rebuild the image, including devcontainers, to apply
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
