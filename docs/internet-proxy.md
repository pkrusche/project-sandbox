# Internet proxy

`--internet-proxy` sends proxy-aware Internet traffic through a separately managed HTTP loopback or remote HTTP/HTTPS proxy while the sandbox firewall prevents direct fallback. Install, start, and configure the proxy separately; Project Sandbox does not manage its lifecycle or Internet destination policy.

[internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally) is one example setup, with a local HTTP listener. See the [endpoint contract](proxy-endpoints.md) for remote endpoints and client compatibility.

Start the external proxy on loopback with an explicit port, then run a Docker sandbox, for example:

```bash
project-sandbox --internet-proxy http://127.0.0.1:18080 --runtime docker --agent bash . python:3.12-slim
```

For a local proxy, Apple `container` uses the same command with `--runtime apple-container`. Configure its administrator-managed localhost DNS entry once, then restart the container system so it rebuilds networking with both the localhost redirect and ordinary container Internet access:

```bash
sudo container system dns create host.docker.internal --localhost 203.0.113.113
container system stop && container system start
```

The DNS/PF change can disrupt container Internet access before the restart and
can disable Private Relay. The CLI verifies the final proxy TCP path from inside
the sandbox, but it never invokes `sudo` or changes this host-wide configuration. If the proxy path later times out instead of being refused, the
alias has gone stale; delete and re-create it as described in
[Apple container host alias](apple-container-dns.md).

### Remote proxies with Apple container

Apple `container`'s bridge network cannot route to a proxy that is only
reachable through the host's VPN. For a remote `--internet-proxy` on Apple
`container`, project-sandbox therefore relays the session through the host,
using the same localhost alias and standard-library forwarder as the
[build proxy](build-network.md#apple-container-host-loopback-forwarder):

```text
sandbox -> http://host.docker.internal:PORT -> host 127.0.0.1:PORT (relay) -> proxy
```

The relay resolves and connects to the proxy from the host, so VPN routes and
DNS apply. The sandbox speaks plain HTTP to the relay. For an `https://` proxy,
the relay opens TLS to the proxy from the host and verifies it with system
trust plus `--ca-cert` files. The host preflight still checks the real proxy
before any image work; the in-sandbox check then verifies the relay path.

`PORT` is derived from the proxy URL (range 40000–48999, distinct from the
build forwarder's port for the same URL). Unlike the build forwarder it cannot
fall back to another port: the sandbox firewall, which is baked into the image,
admits only this address/port pair. The relay binds before any image work, so
a busy port fails fast. Concurrent sessions that use the same remote proxy on
Apple `container` therefore conflict; run them sequentially. The relay listens
only on `127.0.0.1`, runs only while the session runs, and other local
processes can use it meanwhile. Docker and Podman keep connecting to the
remote proxy directly. The generated devcontainer configuration and its
firewall also keep the direct endpoint, because devcontainer tooling runs
outside project-sandbox without the relay.

For corporate access during image construction, use the separate
[`--build-proxy URL` and `--build-ca-cert` options](build-network.md).
They do not grant runtime firewall access.

## Security boundaries

Three independent layers have separate jobs:

- Project Sandbox iptables rules prevent bypass. Proxy environment variables are routing hints, not enforcement.
- The external proxy owns allowed and denied Internet destinations and related security policy.
- `agentgateway-locally` owns AI/MCP routing and provider-credential isolation. Agentgateway is not rerouted through the Internet proxy.

The firewall permits only each configured endpoint’s exact address/port pairs and blocks ordinary public IPv4, public IPv6, and DNS paths. Applications that ignore HTTP proxy variables fail; there is no transparent interception. Publishing a proxy on loopback is useful containment but is not itself the sandbox security boundary.

If the Internet proxy stops, ordinary Internet operations fail while a running Agentgateway or Ollama remains independently reachable. If Agentgateway stops, its AI/MCP operations fail while permitted Internet requests continue through a running Internet proxy. Project Sandbox never restarts either service.

This feature intentionally provides no proxy lifecycle management, transparent interception, TLS interception itself, policy synchronization, or implicit AI/MCP rerouting. Docker and Apple `container` are the primary runtimes. Docker Compose is not part of the setup.

## Web tests that need external hosts

With `--node-npm`, dependencies and browsers are baked into the image, so
ordinary builds and headless tests against a local dev server (`localhost`) need
no network access. Tests that load external hosts (third-party APIs, CDNs, font
or map tiles, staging environments) are blocked by the firewall. Prefer mocking
them (for example Playwright's `page.route`) so tests stay hermetic. When a test
really needs them, either allowlist the hosts directly:

```bash
project-sandbox --node-npm --extra-domain api.example.com --extra-domain cdn.example.com --agent bash /absolute/path/to/repo
```

or route through the Internet proxy, which then owns the destination policy:

```bash
project-sandbox --node-npm --internet-proxy http://127.0.0.1:18080 --runtime docker --agent bash /absolute/path/to/repo
```

`--extra-domain` pins IPs once at startup, which suits a few stable hosts; the
proxy suits many or CDN-backed hosts. The sandbox sets `HTTP_PROXY`,
`HTTPS_PROXY`, and `NO_PROXY`, which Node.js's built-in `fetch`/`http` honour
only with `NODE_USE_ENV_PROXY=1` (other clients need a proxy agent). Browsers
launched by Playwright do not read these variables; pass the proxy explicitly
and keep the local dev server direct:

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

If the proxy intercepts TLS, add its CA with `--ca-cert` (below). Chromium on
Linux reads extra trusted CAs from its NSS database (`~/.pki/nssdb`) rather than
`/etc/ssl/certs`, so either import the CA there in the test setup or set
`ignoreHTTPSErrors` on the browser context for proxied test runs.

## Injecting proxy CA certificates

For a TLS-inspecting corporate proxy, supply its public CA certificate in PEM
format (one certificate per file). Repeat `--ca-cert` for multiple certificates:

```bash
project-sandbox --internet-proxy http://127.0.0.1:18080 \
  --ca-cert /path/to/corporate-root.pem --ca-cert /path/to/intermediate.pem \
  --runtime docker --agent bash . python:3.12-slim
```

Inputs must contain only a PEM certificate and optional surrounding whitespace;
bundles, private keys, end-entity (non-CA) certificates, and unrelated trailing
content are rejected.

`--ca-cert` works with either proxy option, including agent-only private-CA
HTTPS gateways. Remote HTTPS verifies the proxy's certificate and name on the
host and in the sandbox. HTTP remains unencrypted to the proxy. TLS to a proxy
and TLS to a destination are separate connections; see [TLS and trust](proxy-endpoints.md#tls-and-trust).

Certificates are copied into the generated build context under content-based
`.crt` names and baked into the image with `update-ca-certificates`, after all
package installation and project build steps. The installed public certificates
are readable by the unprivileged agent regardless of the host's file creation
permissions. This works with a base image,
`--dockerfile`, and the generated `.devcontainer/`. It configures session trust;
it does not make build-time downloads trust the proxy. For build-time trust, use
the custom Dockerfile `prefix` stage described in [usage](usage.md).

Node is configured with `NODE_USE_SYSTEM_CA=1` to include the OS trust store,
using its [system CA support](https://nodejs.org/api/cli.html#node_use_system_ca1).
No extra certificate bundle environment variables are set. Applications with
private trust stores or explicit TLS configuration may need their own setup.

Only inject CAs you trust: they can authenticate any TLS destination accepted by
applications using this store, including permitted local services. Certificates
are public image contents, not runtime secrets; never pass private keys. Changing
or removing inputs updates the generated files and invalidates the image cache.
Rebuild the image (including a devcontainer rebuild) to apply changes; `--no-build`
continues using the existing image. Dry-run validates certificates and previews
injection without writing files or starting containers.

## Troubleshooting the isolation test

The `internet-proxy-isolation` E2E suite sends a small Responses API request
through Agentgateway, matching Pi and OpenCode, before and after stopping the
Internet proxy. It defaults to `gpt-5-mini`; set `AGENT_PROXY_TEST_MODEL` to select
another gateway model.
The probe uses `max_output_tokens` to bound output and reasoning tokens. It
accepts a completed response or one marked incomplete solely because it reached
that token limit, even if reasoning used the budget before producing text.
Failed responses and other incomplete reasons fail the check.

If Internet checks pass but all three AI checks fail, inspect the `DIAGNOSTIC:`
lines in the suite or session log. They include HTTP status and a bounded error
body, or the connection/parse exception. The gateway key is redacted. These
details are also saved in `result.json` under `gateway_diagnostics` when the test
project is retained with `--keep`. An API rejection can fail this probe even when
an agent session succeeds; check the reported model, parameters, and HTTP error
before changing host networking.

## Independent remote and mixed endpoints

```bash
# Internet-only: no gateway key or model catalog needed.
project-sandbox --internet-proxy https://proxy.corp.example --runtime docker --agent bash . python:3.12-slim
# Both remote endpoints, independently using the default HTTPS port.
project-sandbox --internet-proxy https://proxy.corp.example --agent-proxy https://models.corp.example/v1 --model example-model --runtime docker --agent bash . python:3.12-slim
# Local gateway and remote Internet proxy.
project-sandbox --internet-proxy https://proxy.corp.example --agent-proxy http://127.0.0.1:4000/v1 --model example-model --runtime docker --agent bash . python:3.12-slim
```

Add `--dry-run` to preview effective URLs, routing, bypass entries, and deferred
DNS/TLS checks without secrets, network requests, or generated files. Supply a
model actually exposed by your gateway for a real run. Local Ollama can also
coexist with a remote Internet proxy using `--agent pi --pi-ollama`.

Endpoints are resolved and pinned **inside the sandbox** for the session
(on Apple `container`, a remote Internet proxy is resolved on the host by
[its relay](#remote-proxies-with-apple-container)); restart after DNS changes. Reachability from the host alone is insufficient.
Permissions cover IP/port pairs, including other virtual hosts sharing that pair.
No provider, GitHub, broad gateway, or general DNS exceptions are added. See the
[accepted URL matrix, TLS scope, and client limitations](proxy-endpoints.md).
