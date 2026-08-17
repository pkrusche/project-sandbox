# Internet proxy

`--internet-proxy` sends proxy-aware Internet traffic through a separately managed, host-loopback HTTP proxy while the sandbox firewall prevents direct fallback. Use the external `internet-proxy-locally` project to install, start, and configure that proxy. Project Sandbox does not manage its lifecycle or Internet destination policy.

Start the external proxy on loopback with an explicit port, then run a Docker sandbox, for example:

```bash
project-sandbox --internet-proxy http://127.0.0.1:18080 --runtime docker --agent bash . python:3.12-slim
```

Apple `container` uses the same command with `--runtime apple-container`. Configure its administrator-managed localhost DNS entry once, heed the command's network-connectivity warning, and restart the container system:

```bash
sudo container system dns create host.docker.internal --localhost 203.0.113.113
container system stop && container system start
```

## Security boundaries

Three independent layers have separate jobs:

- Project Sandbox iptables rules prevent bypass. Proxy environment variables are routing hints, not enforcement.
- `internet-proxy-locally` owns allowed and denied Internet destinations and related security policy.
- `agentgateway-locally` owns AI/MCP routing and provider-credential isolation. Agentgateway is not rerouted through the Internet proxy.

The firewall permits only exact forwarded local-service ports and blocks ordinary public IPv4, public IPv6, and DNS paths. Applications that ignore HTTP proxy variables fail; there is no transparent interception. Publishing a proxy on loopback is useful containment but is not itself the sandbox security boundary.

If the Internet proxy stops, ordinary Internet operations fail while a running Agentgateway or Ollama remains independently reachable. If Agentgateway stops, its AI/MCP operations fail while permitted Internet requests continue through a running Internet proxy. Project Sandbox never restarts either service.

This feature intentionally provides no proxy lifecycle management, transparent interception, TLS interception, CA installation, policy synchronization, or implicit AI/MCP rerouting. Docker and Apple `container` are the primary runtimes. Docker Compose is not part of the setup.

## Acceptance procedure

Run these checks with the external proxy's test policy and replace example destinations with its documented permitted and denied fixtures:

1. Confirm permitted HTTP and HTTPS URLs succeed and denied URLs fail through the configured environment.
2. Confirm policy rejects private RFC 1918/ULA, link-local, cloud metadata, explicit IPv4, and explicit IPv6 destinations.
3. From inside the sandbox, verify `curl --noproxy '*' https://example.com`, a shell with all six proxy variables unset, arbitrary public-IP TCP connections, and direct UDP/TCP DNS attempts fail.
4. With Agentgateway and the Internet proxy enabled, stop the Internet proxy first and verify Agentgateway remains reachable; restart it, stop Agentgateway, and verify permitted ordinary Internet access remains. Repeat with `--pi-ollama` and verify Ollama bypass remains port-scoped.
5. Repeat the supported checks on Docker and Apple `container`. These runtime checks require the external proxy setup and are not part of the isolated unit suite.

