# Corporate network access during image builds

Use `--build-proxy URL` for downloads during image construction and
`--build-ca-cert PATH` to install corporate trust before those downloads:

```bash
project-sandbox --build-proxy http://proxy.corp.example:3128 \
  --build-ca-cert /path/to/corporate-root.pem \
  --runtime docker --build-only . python:3.12-slim
```

These options work with base images, `--dockerfile`, `--python-uv`, `--rust-cargo`,
and `--node-npm`. They require an image-based runtime; chroot is unsupported.
Build CAs are included in both generated sandbox and devcontainer Dockerfiles.
They also work independently of a proxy, for example with an internal HTTPS mirror.

## Proxy settings

The URL must be a credential-free HTTP or HTTPS endpoint without a path.
SOCKS and proxy authentication are unsupported. Host proxy variables are not read.
The builder receives lowercase and uppercase `http_proxy`, `https_proxy`, and
`no_proxy` as predefined build arguments. Generated Dockerfiles do not declare
these arguments or persist their values. Custom build commands can still print
or save them; values are also visible in the builder's process arguments.

`no_proxy` defaults to `localhost,127.0.0.1,::1`. Add comma-separated hosts,
`.domain` suffixes (optionally with `:port`), IP addresses, or CIDRs using
repeatable `--build-no-proxy HOSTS`. This requires `--build-proxy`; `*` is rejected.
Bypassed hosts must be reachable directly from the builder, including on VPNs.
Commands that clear their environment, such as default `sudo apt-get`, lose the
proxy settings; run as root or preserve the environment explicitly.

BuildKit excludes undeclared predefined proxy arguments from its layer cache
key and image history. Changing the proxy alone therefore reuses layers.
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
  --build-ca-cert /path/to/corporate-root.pem \
  --internet-proxy http://proxy.corp.example:3128 --runtime docker \
  --agent bash . python:3.12-slim
```

See [Internet proxy configuration](internet-proxy.md) for runtime use and
[the endpoint contract](proxy-endpoints.md) for routing and TLS behavior.

## Builder connectivity

Docker and Podman use the URL verbatim: `127.0.0.1` identifies the builder's own
loopback. Supply an address reachable from the builder or configure its host
networking separately.

### Apple container: host-loopback forwarder

Apple builds reach the proxy through a temporary host-loopback forwarder, so
host VPN routes and DNS apply:

```text
builder -> http://203.0.113.113:PORT -> host loopback forwarder -> proxy
```

Complete the [Apple localhost alias setup](apple-container-dns.md) first.
The builder speaks HTTP to the forwarder. For an HTTPS upstream, the host
verifies TLS using system trust plus `--build-ca-cert` inputs. The forwarder
listens only on loopback during actual builds; cached-image and `--no-build`
runs need none. A busy preferred port falls back to a free port without changing
build inputs. Other host processes can use the forwarder while it is running.

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
