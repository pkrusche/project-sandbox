# Roadmap

This file contains functionality researched but not yet planned for execution
in the next release via <TODO.md>.

## Prebuilt container image distribution

The generated container image is currently intended for local use. Before publishing
or otherwise distributing prebuilt images, complete a third-party licensing and terms
review and make the image carry the notices required by every bundled component.

- Preserve Node.js's license in the image. The generated Dockerfile currently excludes
  `LICENSE` while extracting the official Node.js archive; a distributable image must
  retain or separately reproduce the required copyright and license notice.
- Include the Apache-2.0 license and any applicable notices for the bundled `jj` binary.
- Verify that globally installed npm packages retain their license files and required
  notices, including their transitive production dependencies. Generate and review an
  SBOM and a consolidated third-party-notices file for each released image.
- Treat Claude Code separately from the permissively licensed agents. Its package is
  proprietary and subject to Anthropic's legal agreements; obtain written clarification
  or permission before redistributing an image that embeds it.
- Re-run the review whenever a pinned component or base image changes. Include the base
  image, Debian packages, Node.js, npm packages, downloaded binaries, and generated
  application files in the review scope.
- Never publish an image or layer containing staged user credentials, authentication
  state, project data, build secrets, or local configuration. Build release images from
  a clean, credential-free context and scan the final image before publication.

## Firewall: verify multi-resolver rules on a real iptables host

Code is complete and the render path is covered by
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`.
The unit tests are render-only by policy and do not exercise live iptables.
Outstanding: run the rendered script on a host with iptables and multiple
`nameserver` entries in `resolv.conf`, then confirm allowlisted-domain
pre-resolution works across the resolver setup and post-firewall DNS egress does
not leak before treating this as shipped.

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

## Internet proxy: allow non-loopback proxy hosts

`--internet-proxy` currently accepts only `http://` on `127.0.0.1`, `localhost`, or
`::1` (`internet_proxy.parse`). The restriction is load-bearing rather than cosmetic:
three separate mechanisms assume the proxy listens on host loopback.

- `InternetProxy.forwarded_url` discards the supplied host and rewrites the container's
  `HTTP_PROXY`/`HTTPS_PROXY` to `http://host.docker.internal:{port}`. Only the port
  survives into the sandbox.
- The proxy is registered as a `LocalService`, so `local_service_network.plan()` selects
  a runtime-specific bridge (Apple `container`'s localhost DNS alias, or a `socat`
  listener on Linux bridge runtimes). Both exist only because host loopback is otherwise
  unreachable from inside a container.
- `init-firewall.sh.j2` pins one shared hostname — `local_destinations[0].hostname` — into
  `/etc/hosts` and opens exactly one port-scoped `ACCEPT` rule per destination. Public
  IPv4, public IPv6, and general outbound DNS are dropped, so the container has no route
  to an arbitrary remote address even if the proxy variables are set by hand.

Supporting a remote proxy therefore requires, at minimum:

- Preserve the real host and port in `forwarded_url` for non-loopback hosts, and skip the
  forwarding plan for them instead of bridging a listener that does not exist locally.
- Generalize the firewall template beyond a single pinned hostname so destinations on
  different hosts can each be allowlisted by address and exact port.
- Resolve and pin the proxy hostname at render time, or require a literal IP, because
  allowlisted names are pre-resolved into `/etc/hosts` and general DNS egress is dropped.
- Rework `internet_proxy.preflight`, which currently proves a host-side TCP connect to a
  loopback listener; a remote endpoint needs a reachability check that reflects the path
  the container will actually take.
- Settle the trust model before relaxing anything. Traffic to the proxy is plain `http://`,
  which is acceptable across loopback but sends `CONNECT` requests unencrypted once the hop
  crosses a network. The existing rejection of URL credentials follows from the loopback
  assumption and would need revisiting alongside any transport decision.

Interim workaround that needs no code change: run a loopback forwarder on the host
(`socat`, `stunnel`) pointing at the remote proxy and pass its loopback URL. The sandbox
keeps its port-scoped guarantee and the remote hop stays outside the security boundary.
