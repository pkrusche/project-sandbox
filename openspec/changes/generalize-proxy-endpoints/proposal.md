# Proposal

## Why

Both proxy options currently require HTTP services on host loopback, preventing use of remote gateways and corporate Internet proxies. General endpoint support should preserve independent routing and firewall enforcement while allowing verified HTTPS connections to remote services.

## What Changes

- Support each proxy independently and together; Internet-proxy-only operation already exists and becomes an explicit regression requirement.
- Accept remote HTTP and HTTPS endpoints using DNS names or IPv4/IPv6 literals, preserve remote URL hostnames, and connect directly from the sandbox. Keep existing HTTP loopback forwarding and reject HTTPS loopback endpoints.
- Use default ports 80/443 when omitted, retain agent API paths, and continue rejecting URL userinfo, queries, fragments, and Internet-proxy paths other than `/`.
- Resolve and pin each endpoint for the sandbox lifetime, permitting only its address/port combinations. Keep agent gateways and Ollama outside general Internet proxy routing.
- Verify remote TLS with system trust plus optional public CA certificates supplied through `--ca-cert`; apply relevant trust to host preflight and generated sandbox/devcontainer images, including agent-proxy-only sessions.
- Preserve agent bearer-key authentication. Internet-proxy authentication, client certificates, local HTTPS, network-provider integration, and proxy lifecycle management are out of scope.
- Document compatibility and test independent, mixed local/remote, and HTTPS configurations without tying behavior to a particular corporate network or VPN.

## Capabilities

### New Capabilities

- `proxy-endpoint-connectivity`: Shared endpoint validation, direct versus forwarded routing, DNS pinning, TLS trust, and endpoint reachability checks.

### Modified Capabilities

- `internet-proxy-support`: Extend the existing proxy mode to remote HTTP/HTTPS endpoints, preserve independent operation, and retain enforced routing and failure boundaries.
- `agent-proxy-support`: Generalize accepted endpoints, discovery, firewall rules, and provider configuration to remote HTTP/HTTPS while retaining credentials, supported agents, and model selection behavior.
- `local-ollama-support`: Preserve local forwarding and Internet-proxy bypass when the Internet proxy is remote; limit shared-hostname requirements to local services.

## Impact

Affects URL parsing, CLI orchestration, local-service plan selection, generated firewall scripts, proxy/bypass environment, CA handling, model discovery, devcontainer assets, tests/checkers, and `docs/agent-proxy.md` / `docs/internet-proxy.md`. Remote-only sessions will not require host-loopback forwarding setup. Existing HTTP loopback commands retain their behavior. Firewall changes are security-sensitive; direct fallback must stay blocked in Internet proxy mode. No proxy server or network-provider dependency is introduced.

The previous `add-internet-proxy-support` change was synced and archived as `2026-10-14-add-internet-proxy-support` before completing this proposal. These deltas build on that main-spec baseline.
