# Network access

Sessions have no general Internet access. This page covers how to let traffic
out: the egress modes, an external Internet proxy, the endpoint rules shared by
`--internet-proxy` and `--agent-proxy`, CA certificates, and proxies for image
builds. The model gateway itself is described in [Agent proxy](agent-proxy.md),
the managed hostname proxy in [Egress sidecar](egress-sidecar.md), and the
firewall's guarantees in [Security](security.md#network-firewall).

## Choosing an egress mode

| | Firewall (default) | Your own proxy (`--internet-proxy`) | Sidecar (`--egress sidecar`) |
|---|---|---|---|
| Who decides destinations | `--extra-domain`, pinned to IPs at start | The external proxy | The global `egress.toml`, by hostname |
| Lifecycle | None | Managed by you | One sidecar per runtime (`project-sandbox-egress`) |
| Corporate proxy | — | It is the proxy | `[egress.upstream] url` |

Use the firewall default when a session needs a few fixed domains, an existing
filtering proxy when it should own Internet policy, and the sidecar for one
local hostname allowlist shared by many sandboxes. The sidecar is set up inside
the sandbox exactly like `--internet-proxy http://127.0.0.1:<port>`, with
project-sandbox running the proxy; `--internet-proxy` is rejected in sidecar
mode.

## Internet proxy

`--internet-proxy URL` sends proxy-aware traffic through a separately managed
HTTP or HTTPS proxy, local or remote, while the firewall blocks direct
fallback. project-sandbox doesn't start, stop or configure the proxy;
[internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally) is
one example setup. Docker and Apple container are the primary runtimes; no
Docker Compose setup is involved.

```bash
project-sandbox --internet-proxy http://127.0.0.1:18080 --runtime docker --agent bash . python:3.12-slim
project-sandbox --internet-proxy https://proxy.corp.example --agent-proxy https://models.corp.example/v1 \
  --model example-model --runtime docker --agent bash . python:3.12-slim
```

`--internet-proxy` and `--agent-proxy` are independent; use either or both, and
local Ollama (`--agent pi --pi-ollama`) can be combined with a remote proxy.
`--internet-proxy` rejects `--extra-domain`, `--allow-github`, `--no-firewall`
and the chroot runtime. `--dry-run` previews effective URLs, routing and bypass
entries without secrets, network requests or generated files.

**Responsibilities.** The sandbox firewall prevents bypass; proxy environment
variables are only routing hints. The external proxy owns which destinations
are allowed. The agent gateway owns AI/MCP routing and provider credentials,
and is never routed through the Internet proxy. Applications that ignore
`HTTP_PROXY`/`HTTPS_PROXY` fail; there is no transparent interception. If either
service stops, only its own traffic fails, and project-sandbox restarts neither.

**Preflight.** Before image work the host connects to the proxy once (TCP, plus
TLS verification for HTTPS) so a stopped proxy fails fast. A proxy that only
containers can reach, such as one at a runtime's host-alias address, fails that
check; `--no-internet-proxy-preflight` skips it (not on Apple container). The
sandbox checks the proxy again after its firewall is installed.

**Apple container.** Local proxies are reached through the
[`host.docker.internal` alias](apple-container-dns.md). A remote proxy is
reached through a host relay, so host VPN routes and DNS apply:
`sandbox → http://host.docker.internal:PORT → host relay → upstream`. The relay
listens on loopback for the session on a port derived from the proxy URL, because
the firewall admits exactly that port; concurrent sessions using the same proxy
conflict, sequential ones reuse it. Devcontainers connect directly.

## Endpoint rules

These rules apply to `--internet-proxy` and `--agent-proxy`.

| Endpoint | Accepted | Sandbox route |
| --- | --- | --- |
| `http://127.0.0.1:4000`, `http://localhost:4000`, `http://[::1]:4000` | Yes | Forwarded through `host.docker.internal` |
| HTTPS on any loopback spelling | No | Local TLS forwarding is unsupported |
| `http://proxy.corp.example`, `https://proxy.corp.example` | Yes (ports 80/443 by default) | Direct; HTTPS verified |
| `http://10.20.30.40:8080`, `http://[2001:db8::1]:8080` | Yes | Direct |
| `https://10.20.30.40:8443` | No | HTTPS needs a DNS name |

- **HTTPS needs a hostname.** Node.js, which runs Pi, OpenCode and most agent
  tooling, won't send an IP address as the TLS server name, so HTTPS to an IP
  fails inside the sandbox even when the host preflight passes. Use a DNS name
  with a matching certificate, or plain HTTP to the IP.
- **URL form.** Explicit ports override defaults. Agent API paths such as
  `/api/v1` are kept; Internet proxy paths must be empty or `/`. Userinfo,
  queries, fragments, malformed hosts, scoped IPv6, unspecified and multicast
  addresses are rejected. Private unicast addresses are valid remote endpoints.
- **DNS and firewall.** Before closing DNS, the sandbox resolves each endpoint,
  pins its addresses in `/etc/hosts`, and admits only those address/port pairs.
  Pins last for the session; restart after DNS changes. The firewall
  authorizes an IP and port, so other virtual hosts on that pair are reachable
  too; TLS checks server identity but doesn't make the firewall a hostname
  filter. Names resolving to loopback, unspecified or multicast addresses fail
  startup, as does a failure to enforce IPv6 denial in Internet mode. Apple
  relays pin the relay; the host resolves the upstream per connection.
- **Bypass.** Gateway traffic bypasses the Internet proxy through matching
  `NO_PROXY`/`no_proxy` entries; local gateway and Ollama traffic bypass it via
  `host.docker.internal`.
- **TLS.** Host preflight and the sandbox's final checks verify certificate
  chains and names for direct HTTPS endpoints. Gateway discovery makes a direct,
  authenticated `/models` request and never follows redirects. There is no
  verification bypass, client certificate, proxy authentication (a 407 fails the
  request) or direct fallback. Remote HTTP, including gateway bearer traffic, is
  an explicit unencrypted choice.
- **Tested clients.** curl (HTTP and CONNECT through HTTP and HTTPS proxies),
  Node built-in fetch with `NODE_USE_ENV_PROXY=1`, and Python gateway discovery.
  Clients with their own proxy or trust implementation need their own checks;
  see [development](development.md#proxy-endpoint-verification).

## CA certificates

| Option | Installed in image | Input per file | Also used on the host for |
| --- | --- | --- | --- |
| `--ca-cert` | After dependency and project build steps | One CA certificate | Proxy preflight and the Apple runtime relay |
| `--build-ca-cert` | Before build downloads; kept at runtime | One CA or a CA bundle | The Apple build forwarder |

Both are repeatable and accept public PEM CA certificates only; private keys,
leaf certificates and malformed input are rejected. `--ca-cert` needs
`--internet-proxy` or `--agent-proxy`. Neither changes the host's trust store.
Certificates go into the image's system store (also for `--dockerfile` builds
and devcontainers), and Node gets `NODE_USE_SYSTEM_CA=1`; applications with
private trust stores need their own setup. Only inject CAs you trust: they can
authenticate any TLS destination. Rebuild after changing CA inputs;
`--no-build` keeps the image's existing trust.

Early build CA setup applies to Debian/Ubuntu stages (and custom stages that run
apt or dpkg), keeps the stage's user, and is skipped with a notice for an
inherited non-root user or a non-Debian base.

## Proxies for image builds

`--build-proxy URL` routes downloads during image construction;
`--build-no-proxy HOSTS` (repeatable) adds bypass entries to the default
`localhost,127.0.0.1,::1`. **Build flags don't grant runtime access**: add
`--internet-proxy` for the session too.

```bash
project-sandbox --build-proxy http://proxy.corp.example:3128 \
  --build-ca-cert /path/to/extra-roots.pem --runtime docker --build-only . python:3.12-slim
```

- Works with base images, `--dockerfile`, `--python-uv`, `--rust-cargo` and
  `--node-npm`; not with chroot. `--build-ca-cert` also works without a proxy,
  for example for an internal HTTPS mirror. The URL is a credential-free HTTP or HTTPS
  endpoint without a path; SOCKS and proxy authentication are unsupported, and
  host proxy variables are not read.
- The builder gets the standard `http_proxy`/`https_proxy`/`no_proxy` build
  arguments (both cases). Generated Dockerfiles don't declare or persist them,
  and BuildKit leaves them out of the cache key, so changing only the proxy
  reuses layers. Commands that clear the environment (default `sudo apt-get`)
  lose them. Bypassed hosts must be reachable directly from the builder.
- Docker and Podman use the URL verbatim (`127.0.0.1` is the builder's own
  loopback). Apple builds go through a temporary host-loopback forwarder
  (`builder → http://203.0.113.113:PORT → host → proxy`), so host VPN routes and
  DNS apply; it needs the [Apple alias setup](apple-container-dns.md) and runs
  only during actual builds.
- Devcontainer tools, registry pulls and remote `ADD` downloads build outside
  project-sandbox and need their own proxy configuration (for example Docker's
  `proxies` in `~/.docker/config.json`).

## Tests that need external hosts

With `--node-npm`, dependencies and browsers are in the image, so builds and
headless tests against a local dev server need no network. Prefer mocking
external hosts (Playwright's `page.route`). When a test really needs them,
allowlist a few stable hosts with `--extra-domain`, or route through an Internet
proxy for many or CDN-backed hosts. Node's `fetch`/`http` honour the proxy
variables only with `NODE_USE_ENV_PROXY=1`, and browsers launched by Playwright
not at all; pass the proxy explicitly and keep the dev server direct:

```js
// playwright.config.js
export default {
  use: {
    proxy: process.env.HTTPS_PROXY
      ? { server: process.env.HTTPS_PROXY, bypass: "localhost,127.0.0.1" }
      : undefined,
  },
};
```

For a TLS-intercepting proxy, Chromium on Linux reads extra CAs from its NSS
database (`~/.pki/nssdb`), not `/etc/ssl/certs`; import the CA there in test
setup, or set `ignoreHTTPSErrors` for proxied runs.
