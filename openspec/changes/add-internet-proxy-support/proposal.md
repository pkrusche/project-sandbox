## Why

Proxy environment variables can direct cooperative applications through an Internet filtering proxy, but they do not stop processes from bypassing it with direct network connections. `project-sandbox` needs an opt-in, implementation-neutral mode that combines local proxy forwarding with its existing iptables boundary so ordinary Internet egress fails closed while AI/MCP and other local-service traffic remain independently routed.

## What Changes

- Add `--internet-proxy URL` for a user-managed HTTP proxy listening on a loopback address and require firewall enforcement whenever it is enabled.
- Generalize local-service forwarding so Internet proxy, Agentgateway, and Ollama endpoints use the same runtime-specific forwarding strategies without changing the existing public Ollama or Agentgateway interfaces.
- Inject standard upper- and lowercase proxy environment variables plus a generated local-service `NO_PROXY` list into interactive, headless, Bash, and inherited agent environments.
- Collapse firewall egress in Internet-proxy mode to port-scoped forwarded local services, with no direct public destination, provider, GitHub, extra-domain, or DNS access and fail-closed IPv6 handling.
- Reject incompatible domain-policy flags and keep destination policy and proxy lifecycle in the external `internet-proxy-locally` repository.
- Preflight the configured listener before launching (except in dry-run), without starting, repairing, or depending on a particular proxy implementation.
- Document the separate iptables, Internet-policy, and AI/MCP boundaries and add CLI, environment, firewall, forwarding, and acceptance coverage.

## Capabilities

### New Capabilities

- `internet-proxy-support`: Defines validation, forwarding, environment, firewall enforcement, preflight, coexistence, failure, and documentation behavior for the opt-in filtering-proxy mode.

### Modified Capabilities

- `agent-proxy-support`: Requires Agentgateway to coexist with the Internet proxy as an independently forwarded, port-scoped local service and to bypass general proxy routing.
- `local-ollama-support`: Requires local Ollama to coexist with the Internet proxy as an independently forwarded local service and to bypass general proxy routing.

## Impact

- Affects CLI validation and dry-run output, local-service network setup, container command/environment construction, firewall rendering, templates, runtime preflight, tests, and `docs/internet-proxy.md`.
- Preserves behavior when `--internet-proxy` is absent and preserves the existing `--agent-proxy` and `--pi-ollama` command-line interfaces.
- Introduces no dependency on Pipelock, Smokescreen, Docker Compose, transparent interception, TLS interception, or external-proxy lifecycle management.
