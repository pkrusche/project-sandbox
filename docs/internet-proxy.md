# Internet proxy

`--internet-proxy` sends proxy-aware Internet traffic through a separately managed, host-loopback HTTP proxy while the sandbox firewall prevents direct fallback. Install, start, and configure the proxy separately; Project Sandbox does not manage its lifecycle or Internet destination policy.

[internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally) is one example setup, but any HTTP proxy will do.

Start the external proxy on loopback with an explicit port, then run a Docker sandbox, for example:

```bash
project-sandbox --internet-proxy http://127.0.0.1:18080 --runtime docker --agent bash . python:3.12-slim
```

Apple `container` uses the same command with `--runtime apple-container`. Configure its administrator-managed localhost DNS entry once, then restart the container system so it rebuilds networking with both the localhost redirect and ordinary container Internet access:

```bash
sudo container system dns create host.docker.internal --localhost 203.0.113.113
container system stop && container system start
```

The DNS/PF change can disrupt container Internet access before the restart and
can disable Private Relay. The CLI verifies the final proxy TCP path from inside
the sandbox, but it never invokes `sudo` or changes this host-wide configuration.

## Security boundaries

Three independent layers have separate jobs:

- Project Sandbox iptables rules prevent bypass. Proxy environment variables are routing hints, not enforcement.
- The external proxy owns allowed and denied Internet destinations and related security policy.
- `agentgateway-locally` owns AI/MCP routing and provider-credential isolation. Agentgateway is not rerouted through the Internet proxy.

The firewall permits only exact forwarded local-service ports and blocks ordinary public IPv4, public IPv6, and DNS paths. Applications that ignore HTTP proxy variables fail; there is no transparent interception. Publishing a proxy on loopback is useful containment but is not itself the sandbox security boundary.

If the Internet proxy stops, ordinary Internet operations fail while a running Agentgateway or Ollama remains independently reachable. If Agentgateway stops, its AI/MCP operations fail while permitted Internet requests continue through a running Internet proxy. Project Sandbox never restarts either service.

This feature intentionally provides no proxy lifecycle management, transparent interception, TLS interception itself, policy synchronization, or implicit AI/MCP rerouting. Docker and Apple `container` are the primary runtimes. Docker Compose is not part of the setup.

## Injecting proxy CA certificates

For a TLS-inspecting corporate proxy, supply its public CA certificate in PEM
format (one certificate per file). Repeat `--ca-cert` for multiple certificates:

```bash
project-sandbox --internet-proxy http://127.0.0.1:18080 \
  --ca-cert /path/to/corporate-root.pem --ca-cert /path/to/intermediate.pem \
  --runtime docker --agent bash . python:3.12-slim
```

`--ca-cert` requires `--internet-proxy` and its enforced firewall. The proxy URL
must still use host loopback; installing a CA does not encrypt the HTTP hop to
the proxy or enable remote proxy hosts. The external proxy performs TLS
inspection and owns destination policy.

Certificates are copied into the generated build context under content-based
`.crt` names and baked into the image with `update-ca-certificates`, after all
package installation and project build steps. This works with a base image,
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
