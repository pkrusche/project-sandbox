# Proxied network access during image builds

See [concurrent builds](concurrent-builds.md) for context locking, cache reuse,
and image selection when several CLI invocations run together.

Use `--build-proxy URL` for downloads during image construction and
`--build-ca-cert PATH` to install additional certificates to trust (e.g. in a
corporate network) before those downloads:

```bash
project-sandbox --build-proxy http://proxy.corp.example:3128 \
  --build-ca-cert /path/to/extra-roots.pem \
  --runtime docker --build-only . python:3.12-slim
```

These options work with base images, `--dockerfile`, `--python-uv`, `--rust-cargo`,
and `--node-npm`. They require an image-based runtime; chroot is unsupported.
Build CAs are included in both generated sandbox and devcontainer Dockerfiles.
They also work independently of a proxy, for example with an internal HTTPS mirror.

With `--egress sidecar`, sidecar image builds also use `--build-proxy`,
`--build-no-proxy`, and `--build-ca-cert`. The Alpine sidecar installs build CAs
before its first package download and retains them in its system trust store.
CA contents contribute to the sidecar image tag; proxy URLs do not. Cached
sidecar images need no proxy connection or Apple forwarder. These settings
apply to sidecar builds launched by `project-sandbox`; the separate
`project-sandbox-egress` management CLI does not accept build-network options.

## Proxy settings

The URL must be a credential-free HTTP or HTTPS endpoint without a path.
SOCKS and proxy authentication are unsupported. Host proxy variables are not read.
The builder receives lowercase and uppercase `http_proxy`, `https_proxy`, and
`no_proxy` as build arguments. Every generated build stage that runs commands
declares them with `ARG` (no value) right after `FROM`, so `RUN` steps see them
as environment variables on every builder. This includes the egress sidecar and
all stages from `--dockerfile`, `--python-uv`, `--rust-cargo`, and `--node-npm`.
Podman/Buildah, unlike BuildKit, does not export undeclared proxy arguments.
Without `--build-proxy` the arguments stay unset. Values are never written to
`ENV`, so the image's runtime environment has no build proxy. Custom build
commands can still print or save them. Values are also visible in the builder's
process arguments and may appear in image history for `RUN` steps.

`no_proxy` defaults to `localhost,127.0.0.1,::1`. Add comma-separated hosts,
`.domain` suffixes (optionally with `:port`), IP addresses, or CIDRs using
repeatable `--build-no-proxy HOSTS`. This requires `--build-proxy`; `*` is rejected.
Bypassed hosts must be reachable directly from the builder, including on VPNs.
Commands that clear their environment, such as default `sudo apt-get`, lose the
proxy settings; run as root or preserve the environment explicitly.

Declared arguments are part of the `RUN` layer cache key. Changing the proxy
URL, or the Apple forwarder falling back to another port, therefore rebuilds
`RUN` layers on the next actual build. It does not change the rendered
Dockerfiles, so an image that is already up to date is reused without a build.
Changing CA contents invalidates generated build inputs. Removing CA options
removes their staged files and generated setup on the next render. Rebuild to
apply trust changes; `--no-build` retains the existing image.

## Build trust and runtime trust

| Option | Installed in image | Input per file | Host-side TLS use |
| --- | --- | --- | --- |
| `--build-ca-cert` | Before build downloads; retained at runtime | One CA or a CA bundle | Apple build forwarder |
| `--ca-cert` | After dependency and project build steps | One CA | Runtime proxy preflight and Apple runtime relay |

Both options are repeatable and accept public PEM CA certificates only. Private
keys, leaf certificates, and malformed input are rejected. `--ca-cert` requires
`--internet-proxy` or `--agent-proxy`; `--build-ca-cert` has no such requirement.
Neither option changes the host's system trust store.

Early CA setup applies to executable Debian/Ubuntu stages, including `prefix`.
Known Debian/Ubuntu image names and release tags qualify, as do custom stages
or their parents that invoke apt or dpkg. Unknown or non-Debian bases are skipped
with a warning; stages without `RUN` commands are skipped silently.

The setup preserves the stage's user. It follows an explicit `USER root` before
the first `RUN`, otherwise runs just after `FROM`. An inherited non-root user
causes installation to be skipped with a notice. System roots are retained and
certificate verification stays enabled, including when bootstrapping missing
CA tools. Node, Python requests, and uv receive the extended system bundle;
applications with private trust stores may need their own configuration.

**Build flags do not grant runtime firewall access.** To enable runtime proxy
access, add `--internet-proxy` and build an image with that policy:

```bash
project-sandbox --build-proxy http://proxy.corp.example:3128 \
  --build-ca-cert /path/to/extra-roots.pem \
  --internet-proxy http://proxy.corp.example:3128 --runtime docker \
  --agent bash . python:3.12-slim
```

See [Internet proxy configuration](internet-proxy.md) for runtime use and
[the endpoint contract](proxy-endpoints.md) for routing and TLS behavior.

## Builder connectivity

Docker and Podman use the URL verbatim: `127.0.0.1` identifies the builder's own
loopback. Supply an address reachable from the builder or configure its host
networking separately.

### Proxy hostnames and musl-based tools

A proxy hostname is resolved inside the build container by each tool that uses
it. Tools linked against musl resolve names differently from glibc: musl asks
all nameservers in `resolv.conf` at once and takes the first answer, and does
not use `nsswitch.conf`. Under some VM and VPN DNS setups, notably Colima on a
corporate network, musl tools then fail to resolve a proxy hostname that glibc
tools in the same build resolve without trouble. Affected steps include:

- `uv sync` in `--python-uv` builds (the pinned uv binary is statically linked
  against musl),
- `apk` in the Alpine `--egress sidecar` image build,
- any Alpine-based stage of a `--dockerfile` build.

The error is a DNS failure that names the download target rather than the proxy,
for example uv's `error sending request for url (https://pypi.org/...)` caused
by `dns error: failed to lookup address information`. Debian steps such as
`apt-get`, `curl`, and `npm` earlier in the same build succeed.

To confirm, compare both resolvers in a build on the same engine:

```bash
cat > dns-check.Dockerfile <<'EOF'
FROM python:3.12-slim AS glibc
RUN cat /etc/resolv.conf; getent hosts proxy.corp.example || echo "glibc FAILED"
FROM alpine:3.20
RUN getent hosts proxy.corp.example || echo "musl FAILED"
EOF
docker --context colima-project-sandbox build --no-cache --progress=plain \
  -f dns-check.Dockerfile .
```

Workaround: pass the proxy as an IP address, which needs no lookup:

```bash
project-sandbox --runtime colima --python-uv \
  --build-proxy http://192.0.2.10:3128 .
```

For an HTTPS proxy this only works if its certificate lists that IP address;
otherwise use an HTTP proxy endpoint or fix the builder's DNS configuration.

project-sandbox does not work around this itself. A possible future fix is to
resolve the proxy hostname on the host and pass `--add-host <host>:<ip>` to
Docker and Podman builds. musl reads `/etc/hosts` before DNS, and the URL would
keep its hostname, so TLS verification of an HTTPS proxy still works.

### Apple container: host-loopback forwarder

Apple builds reach the proxy through a temporary host-loopback forwarder, so
host VPN routes and DNS apply:

```text
builder -> http://203.0.113.113:PORT -> host loopback forwarder -> proxy
```

Complete the [Apple localhost alias setup](apple-container-dns.md) first.
The builder speaks HTTP to the forwarder. For an HTTPS upstream, the host
verifies TLS using system trust plus `--build-ca-cert` inputs. The forwarder
listens only on loopback during actual builds. Cached images need none;
`--no-build` skips the sandbox build but can still build a missing sidecar image.
A busy preferred port falls back to a free port without changing
the rendered Dockerfiles, though layers built with it miss the cache. Other host processes can use the forwarder while it is running.

## Devcontainers and external downloads

Devcontainer tools build outside project-sandbox and need their own proxy
configuration, such as Docker's `proxies` settings in `~/.docker/config.json`.
The generated `devcontainer.json` contains no build proxy arguments because it
can be copied into image layers by a whole-project build.

Registry pulls and remote `ADD` downloads run outside image stages and need
separate daemon/builder proxy and trust configuration. Allow all package hosts
and redirects needed by the build in your proxy's policy.

`--dry-run` validates and previews these settings without writing files,
listening, or contacting the proxy. For acceptance tests, see
[Squid build-network verification](development.md#squid-build-network-verification).
