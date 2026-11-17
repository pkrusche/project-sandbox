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

## VCS defaults and isolated host capture

Finding #2 is closed for the VCS session-mode scope. Two remaining risks are
accepted for this release and need future work:

- **Writable metadata remains the default.** `--vcs-access full` preserves
  read/write Git/jj metadata access. Evaluate a restricted default or explicit
  opt-in to full access, including compatibility and migration for agents that
  commit inside the sandbox. Restricted modes already protect existing metadata
  but are not snapshots against other host processes or concurrent full sessions.
- **Host capture can still execute workspace-controlled code.** Host
  finalization (`worktree.finalize`: `git status`, `git add -A`, `git commit`)
  runs Git with the repository's own configuration. Two paths let the agent
  choose what runs there:
  - With the default `--vcs-access full`, the agent can edit `.git/config`
    directly, setting `core.fsmonitor`, `core.hooksPath`, `filter.<name>.clean`
    or signing programs that the host then executes. jj's repository config is
    exposed the same way.
  - With read-only or hidden metadata, tracked `.gitattributes` can still route
    files through filter drivers already defined in trusted config, and hooks
    already configured on the host can run scripts that live in the workspace.

  Per-call `-c` overrides cannot close this. They cannot enumerate arbitrary
  `filter.<name>.*` keys, and `core.hooksPath=/dev/null` also disables the
  user's own hooks and signing hooks. `-c core.fsmonitor=false` is a cheap
  interim step with no user-visible cost (tracked in TODO.md); hooks, filters
  and signing stay here. The real fix is private VCS stores with sandbox-side
  capture and trusted object import, alongside the separate-clone work above.
  Audit hooks, filters, fsmonitor, signing and other executable configuration
  surfaces, and verify with inert marker hooks/helpers that no agent-controlled
  code executes on the host during capture.

See [VCS security](docs/security.md#vcs-metadata-access) and
[VCS access limitations](docs/vcs-access.md#scope-and-remaining-risks).

## Egress sidecar follow-ups

- **Per-sandbox denial attribution** (egress-sidecar-macos). Sandboxes reach the
  sidecar through the runtime's port forwarder, so Squid's `client` address does
  not identify a sandbox, and the session-end summary counts the denials of
  every client of the sidecar during the session's window. Attribution needs a
  per-session identity on the proxy connection, for example a per-session
  listener or proxy credential.
- **Per-sandbox denial attribution from internal-network source IPs**
  (isolated-sidecar-network). On Colima's isolated network, sandboxes reach
  Squid directly, so each connection carries the sandbox's own address and no
  port forwarder sits in between. Spike S6 (`spikes.md` in that change) found
  distinct per-sandbox addresses in Squid's access log on both Colima and
  Apple `container`'s internal network. Sequential sandboxes reuse addresses,
  though, so the launch must record the sandbox's internal address *and* the
  session's time window. The session-end summary can then filter denials by
  both, with no per-session listener or credential.
- **Upstream proxy authentication** (egress-sidecar-macos). Basic, NTLM or
  Kerberos to the `[egress.upstream]` parent. Today a 407 from the parent fails
  the request.
- **Business and enterprise Copilot hosts** (egress-sidecar-macos). Capture them
  with `scripts/e2e-copilot-egress.py`: `api.{business,enterprise}.githubcopilot.com`
  are admitted by analogy with the individual-plan capture (egress-sidecar-macos
  design.md Decision 5).
- **GHE.com tenants for `--copilot-host` in sidecar mode** (egress-sidecar-macos;
  rejected today). Capture the tenant host set and add a tenant profile.

## Namespace-owner gateway for Linux sidecar mode

Deferred by `isolated-sidecar-network`, which makes the container network the
containment boundary only on Colima. On Docker Engine,
rootless Podman, Docker Desktop and Podman machine, sidecar-mode sandboxes
still hold `NET_ADMIN`/`NET_RAW` and contain themselves with their own
firewall.

- A per-session privileged gateway container owns the session's network
  namespace and installs its nftables policy: default-deny, with only the
  sidecar endpoint and this session's local-service ports admitted.
- The sandbox joins that namespace with `--network container:<gateway>` and
  holds no network capabilities, so nothing running in the sandbox can change
  the rules that contain it.
- The gateway does nothing after setup and is removed with the session.
- This isn't possible on Apple `container`: each container is its own VM, and
  there is no way to share a network namespace between containers.

## Isolated sidecar network on Apple `container`

Dropped from `isolated-sidecar-network` after its spikes (S3–S5 in that change's
`spikes.md`). Apple `container` keeps the published endpoint, and its sandbox
firewall stays the containment boundary, because:

- its `--internal` network is host-only: a sandbox on it reaches every Mac
  service that listens on all interfaces, and every Mac loopback service through
  the `203.0.113.113` redirect behind `host.docker.internal`, which the sidecar
  itself needs;
- `container run` has no `--add-host`, so `egress` can't be pinned without DNS;
- a container's address changes on every stop/start and recreation.

It becomes possible with packet-filter rules on the Mac for the host-only
network (administrator rights, which project-sandbox never uses), or with a
future `container` network mode that has no host path.

## GitHub Copilot agent follow-ups

Deferred by `add-copilot-agent-support`:

- **Self-hosted GitHub Enterprise Server (GHES) hosts for `--copilot-host`**
  (currently rejected): needs its own host/endpoint model and firewall entries.
- **Transcript renderer from real captures.** Tighten the Copilot transcript
  renderer and exit reclassification from real e2e captures
  (`scripts/e2e-copilot.py --capture-dir`): replace the synthetic JSONL fixtures
  in `tests/test_transcript.py` with captured ones, and confirm the record kinds
  in `transcript.py` and `observability.copilot_json_stream_failure`.

## Remote proxy endpoints (implemented)

Both proxy options now share HTTP loopback and remote HTTP/HTTPS endpoint
validation. Remote endpoints connect directly; only local services use runtime
forwarding. Sandbox startup resolves and pins each endpoint separately and
permits its exact address/port pairs, with verified TLS on the host and through
the final firewall. Optional public CAs apply with either proxy.

See [proxy endpoints](docs/proxy-endpoints.md) for supported URLs, session DNS
lifetime, trust, and compatibility; [development](docs/development.md#proxy-endpoint-verification)
records verification and runtime acceptance limitations. Deferred authentication
and dynamic DNS refresh are listed below.

Items still to be considered:

- Let concurrent Apple container sessions share one `--internet-proxy` relay; today the fixed, firewall-baked relay port makes a second session using the same proxy fail.
- Consider authenticated Internet proxies without exposing credentials or forwarding gateway keys; this release intentionally rejects URL credentials and has no authentication negotiation.
- Consider privileged DNS refresh for long sessions; current endpoint pins require a session restart after DNS changes.
- Run the opt-in `proxy-endpoints` suite on Docker, Podman, and Apple container, including its native IPv6 scenario. Separately verify pinned bundled Pi/OpenCode versions, DNS-name pinning, and IPv6-disabled fallback. The implementation environment has no container runtime or network-administration capabilities.
- A remote `--agent-proxy` on Apple container has the same VPN routing gap as `--internet-proxy` had; consider relaying it through the host as well.

## Additional hardening

- **Mount encoding:** CLI bind mounts use CSV encoding, but devcontainer mount
  strings still need consistent runtime encoding. Verify live parse/render round trips for unusual project names (including commas, quotes,
  dollars, spaces, and wildcards) on each runtime, and fail closed for unsupported
  control characters and mount forms. Keep raw user mount parsing aligned with
  runtime grammar; JSON or argv escaping alone does not cover nested syntax.
- **Host lock files:** `host_locks.path_lock()` correctly uses `O_NOFOLLOW` and
  avoids truncation, but does not verify the opened inode is a regular file
  owned by the caller. Names are predictable in shared temporary storage. Use a
  verified private per-user lock directory, `fstat()` checks, and bounded waits
  with useful diagnostics. Existing OS protections may limit cross-user attacks;
  do not depend on identical behavior across Linux and macOS. Never unlink a
  live lock file to recover it, since that can create two independently locked
  inodes.
- **Sensitive logs:** Squid's `%ru` logging includes URLs/query strings for
  visible HTTP and intercepted requests; redact sensitive fields and define
  retention/access rules. Token substitution in logs is useful but is not a
  general data-loss prevention boundary.
- **Supply chain:** exact top-level npm versions are useful, but global
  `npm install -g package@version` does not freeze all transitive dependency
  resolution. Use reviewed lockfiles/integrity metadata or reproducible tool
  layers, and define a deliberate image/security-update cadence. Project build
  scripts and user-supplied Dockerfiles remain trusted image-producing code.

## Podman machine shared-folder staleness

Observed 2026-11-11 on macOS with Podman 6.0 (Podman machine VM): the firewall e2e suite's second container failed with `FileNotFoundError` creating `/workspace/firewall.ok` after the host had deleted that file following the first session. Overwriting files that still existed worked. Unconfirmed cause: the VM's shared-folder (virtiofs) cache still lists the deleted file across containers. The suite now writes one marker per session, which avoids the case but does not fix it. Users hit the same problem if they delete a workspace file on the host between Podman sessions and the agent re-creates it. Reproduce outside the suite (create a file in one container, delete it on the host, create it again in a fresh container); if it reproduces, document it in `docs/` and check whether the machine's cache mode or a mount option avoids it.
