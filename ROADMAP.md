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

`--egress sidecar` (change `egress-sidecar-macos`) blocks GitHub repository
access and admits only the Copilot CLI's hosts. The next step is per-repository
access, so an agent can work on the repositories it needs without getting all of
GitHub. The squid-sidecar line drafted this design and deferred it.

- **Policy:** `[egress.github]` with `read` and `write` lists of `owner/repo` or
  `owner/*` (`write` implies `read`). Components are validated against
  `^[A-Za-z0-9_.-]+$` so they are regex-safe. Optional `graphql = true`.
- **Interception:** bump `github.com`, `api.github.com`,
  `raw.githubusercontent.com` and `codeload.github.com` only when repository
  rules are active, with the sidecar CA name-constrained to those hosts.
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

## Egress sidecar follow-ups

Deferred by `egress-sidecar-macos` (one published Squid sidecar per runtime):

- **Linux runtimes** (`egress-sidecar-linux`, proposed). Docker Engine and
  rootless Podman on a Linux host are rejected today: a port published on host
  loopback does not reach their sandboxes through the existing local-service
  forwarding.
- **Per-sandbox denial attribution** (egress-sidecar-macos). Sandboxes reach the
  sidecar through the runtime's port forwarder, so Squid's `client` address does
  not identify a sandbox, and the session-end summary counts the denials of
  every client of the sidecar during the session's window. Attribution needs a
  per-session identity on the proxy connection, for example a per-session
  listener or proxy credential.
- **Upstream proxy authentication** (egress-sidecar-macos). Basic, NTLM or
  Kerberos to the `[egress.upstream]` parent. Today a 407 from the parent fails
  the request.

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
