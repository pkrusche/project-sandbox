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

This feature intentionally provides no proxy lifecycle management, transparent interception, TLS interception, CA installation, policy synchronization, or implicit AI/MCP rerouting. Docker and Apple `container` are the primary runtimes. Docker Compose is not part of the setup.
