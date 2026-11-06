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

## Smart GitHub repository access in sidecar egress mode

`--egress sidecar` (change `add-squid-egress-sidecar`) blocks GitHub repository
access and admits only the Copilot CLI's hosts. The next step is per-repository
access, so an agent can work on the repositories it needs without getting all of
GitHub. The design below was drafted for that change and deferred.

- **Policy:** `[egress.github]` with `read` and `write` lists of `owner/repo` or
  `owner/*` (`write` implies `read`). Components are validated against
  `^[A-Za-z0-9_.-]+$` so they are regex-safe. Optional `graphql = true`.
  `--allow-github` in sidecar mode would mean every repository, with GraphQL
  still denied by default.
- **Interception:** bump `github.com`, `api.github.com`,
  `raw.githubusercontent.com` and `codeload.github.com` only when repository
  rules are active, using a per-session CA name-constrained to those hosts, with
  the CA installed into sandbox trust at runtime.
- **Rules** (`url_regex`/`urlpath_regex` combined with `method` ACLs,
  case-insensitive):

  | Traffic | Path pattern | Rule |
  |---|---|---|
  | git fetch | `github.com/<o>/<r>(.git)?/info/refs?service=git-upload-pack`, `POST …/git-upload-pack` | read |
  | git push | `…/info/refs?service=git-receive-pack`, `POST …/git-receive-pack` | write |
  | LFS | `github.com/<o>/<r>.git/info/lfs/…` | read / write (see open question) |
  | REST | `api.github.com/repos/<o>/<r>(/…)?` | GET/HEAD → read; other methods → write |
  | raw / archives | `raw.githubusercontent.com/<o>/<r>/…`, `codeload.github.com/<o>/<r>/…` | read, GET only |
  | GraphQL | `api.github.com/graphql` | deny unless opted in |
  | Exfil channels | `api.github.com/gists…`, `gist.github.com`, `POST /user/repos`, `POST /orgs/<o>/repos`, `/repos/<o>/<r>/forks` | deny |
  | Web UI | other `github.com` paths | deny |
  | Signed asset hosts | `objects.githubusercontent.com`, `github-cloud.githubusercontent.com`, `release-assets.githubusercontent.com`, LFS media host | spliced, hostname-only |

- **Open questions:**
  - Confirm the URL shapes by capturing `git clone`, `git push`, `git lfs pull`
    and `gh release download` through an intercepting proxy, the same way
    `scripts/e2e-copilot-egress.py` captures Copilot.
  - LFS download and upload likely share the batch endpoint
    (`POST …/info/lfs/objects/batch`) and differ only in the JSON body, which
    Squid cannot inspect. Either treat LFS batch as `write`, or add body
    inspection.
  - `gh pr create` and `gh issue` use GraphQL, which this design denies by
    default.
  - Credential injection: the sidecar could add a GitHub token for allowed
    repository paths, so the sandbox holds no token.

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

- Consider authenticated Internet proxies without exposing credentials or forwarding gateway keys; this release intentionally rejects URL credentials and has no authentication negotiation.
- Upstream (parent) proxy authentication (Basic, NTLM, Kerberos). Today an HTTP
  407 from the parent fails the request.
- Consider privileged DNS refresh for long sessions; current endpoint pins require a session restart after DNS changes.
- Run the opt-in `proxy-endpoints` suite on Docker, Podman, and Apple container, including its native IPv6 scenario. Separately verify pinned bundled Pi/OpenCode versions, DNS-name pinning, and IPv6-disabled fallback. The implementation environment has no container runtime or network-administration capabilities.
- A remote `--agent-proxy` on Apple container has the same VPN routing gap as `--internet-proxy` had; consider relaying it through the host as well.

## Global egress sidecar follow-ups

Deferred by `global-egress-sidecar-macos` (one global sidecar per runtime):

- **Per-sandbox denial attribution** (global-egress-sidecar-macos). The access
  log records each request's client IP, but the session-end summary counts the
  denials of every sandbox on the sidecar during the session's window. Map
  client IPs on `sbx-egress` to sessions (from `inspect` at launch) so each
  session reports only its own denials.
- **Pin the sidecar's IP so sandboxes survive restarts**
  (global-egress-sidecar-macos). `project-sandbox-egress restart` can give the
  sidecar a new address on `sbx-egress`, which running sandboxes have baked into
  `/etc/hosts` and their firewall. A fixed address (`--ip` on Docker/Podman; an
  equivalent on Apple `container`) would let them keep working.
- **A shared host relay for firewall-mode Apple sessions**
  (global-egress-sidecar-macos). In sidecar mode the sidecar owns one detached
  relay to a remote parent proxy. Firewall-mode `--internet-proxy` sessions on
  Apple `container` still bind a fixed per-proxy port in the CLI process, so a
  second concurrent session using the same proxy fails; they could share a
  detached relay the same way.
