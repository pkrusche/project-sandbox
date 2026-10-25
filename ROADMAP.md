# Roadmap

This file contains functionality researched but not yet planned for execution
in the next release via <TODO.md>.

## Isolate concurrent subagents in separate clones, merge back on teardown

Every `--branch` jj agent shares one repo's `.jj/repo` store and — since we now
also mount the git backend — its `.git`, both bind-mounted read-write into each
container. That fits jj's concurrent-workspace model on a shared-kernel runtime,
but concurrent writes from *inside* multiple containers to a single shared store
are not obviously safe across separate VMs (Apple `container` + VirtioFS), where
lock-file and rename atomicity may not hold.

Plan: give each subagent its own clone, then merge/rebase the agent's bookmark 
back into the parent repo during teardown. This removes the shared-store race 
entirely and keeps each agent's blast radius isolated.

Note the git-worktree (`--branch` non-jj) path — which shares `.git` the same way —
should use the same approach.

Interim mitigation already in place: one host-side exclusive lock
(`_workspace_lock`) serializes workspace setup, finalization, and build-failure
cleanup, so those shared-store mutations cannot interleave. It does not address
concurrent in-container writes; this item supersedes it.

## Remote proxy endpoints (implemented)

Both proxy options now share HTTP loopback and remote HTTP/HTTPS endpoint
validation. Remote endpoints connect directly; only local services use runtime
forwarding. Sandbox startup resolves and pins each endpoint separately and
permits its exact address/port pairs, with verified TLS on the host and through
the final firewall. Optional public CAs apply with either proxy.

See [proxy endpoints](docs/proxy-endpoints.md) for supported URLs, session DNS
lifetime, trust, and compatibility; [development](docs/development.md#proxy-endpoint-verification)
records verification and runtime acceptance limitations. Deferred authentication
and dynamic DNS refresh are tracked in TODO.md.

Items still to be considered:

- Let concurrent Apple container sessions share one `--internet-proxy` relay; today the fixed, firewall-baked relay port makes a second session using the same proxy fail.
- Consider authenticated Internet proxies without exposing credentials or forwarding gateway keys; this release intentionally rejects URL credentials and has no authentication negotiation.
- Consider privileged DNS refresh for long sessions; current endpoint pins require a session restart after DNS changes.
- Run the opt-in `proxy-endpoints` suite on Docker, Podman, and Apple container, including its native IPv6 scenario. Separately verify pinned bundled Pi/OpenCode versions, DNS-name pinning, and IPv6-disabled fallback. The implementation environment has no container runtime or network-administration capabilities.
- A remote `--agent-proxy` on Apple container has the same VPN routing gap as `--internet-proxy` had; consider relaying it through the host as well.
