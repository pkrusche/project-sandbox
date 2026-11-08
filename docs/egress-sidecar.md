# Egress sidecar (`--egress sidecar`)

`--egress sidecar` sends all of a sandbox's Internet traffic through the
runtime's **global** [Squid](https://www.squid-cache.org/) sidecar. The sidecar
filters by **hostname**, not by IP address. The default `--egress firewall` mode
resolves each allowed domain once at start-up and pins the addresses. That
breaks when CDNs rotate addresses, admits unrelated hosts that share an IP, and
cannot express wildcards. Sidecar mode avoids those problems:

- Every request is checked against a hostname allowlist, with `*.example.com`
  wildcards, and names are resolved per request.
- For HTTPS, the TLS SNI must match the CONNECT host, and non-TLS bytes in a
  tunnel are dropped.
- IP-literal destinations and ports other than 443 (HTTPS) and 80 (HTTP) are
  refused.
- In direct mode, hosts that resolve to private, loopback, link-local or cloud
  metadata addresses are refused.
- GitHub repository access is blocked. The Copilot CLI still works through a
  narrow Copilot profile.
- An existing corporate proxy can sit behind the sidecar as its parent.

The mode is opt-in. Firewall mode is unchanged and remains the default.

```bash
project-sandbox-egress allow add pypi.org      # once, for every sidecar sandbox
project-sandbox --agent claude --egress sidecar .
```

Sidecar mode works on Apple `container` (1.4.1 or later), Docker Desktop and
Podman machine. On Docker Engine and rootless Podman on a Linux host it is
rejected before any container work (see `egress-sidecar-linux` in
[ROADMAP.md](../ROADMAP.md)).

## Choosing an egress mode

| | Firewall (default) | Sidecar (`--egress sidecar`) | Your own proxy (`--internet-proxy`) |
|---|---|---|---|
| Policy | Domains pinned to IPs at start | Hostnames per request, wildcards | Whatever the proxy enforces |
| Where policy lives | Flags per launch (`--extra-domain`) | One global `egress.toml` | The proxy |
| Live changes | Relaunch | `project-sandbox-egress allow add` | The proxy's own tooling |
| Lifecycle | None | One sidecar per runtime, managed by `project-sandbox-egress` | Managed by you |
| Combine with a corporate proxy | — | `[egress.upstream] url` | It is the proxy |
| GitHub | `--allow-github` | Blocked (Copilot profile only) | The proxy |

Use firewall mode when a session needs only a few fixed domains or GitHub. Use
the sidecar for one hostname policy that many sandboxes share. Use
`--internet-proxy` when an existing filtering proxy should decide on its own
(see [Internet proxy](internet-proxy.md)). The sidecar can also put a local
allowlist in front of such a proxy ([Parent proxy](#parent-proxy)).

## How it works

```
 sandbox (default network)                      host                       sidecar (default network)
 HTTP(S)_PROXY=http://host.docker.internal:3129
 firewall: DROP, admit the sidecar endpoint ──▶ 127.0.0.1:3129 ──publish──▶ squid :3128 ──▶ Internet / parent
           + this session's local services  ──▶ 127.0.0.1:8787, :11434 (agent proxy, Ollama)
```

Each runtime has **one** sidecar container, `project-sandbox-egress`, on the
runtime's default network. It publishes Squid's port on the host's loopback
address only, on `[egress.sidecar] port` (default `3129`; 3128 is often taken
by a host Squid).

A sidecar-mode sandbox is a firewall-mode sandbox with an Internet proxy: it is
launched exactly as with `--internet-proxy http://127.0.0.1:3129`.

- **Firewall.** The sandbox's firewall drops everything except TCP to the
  sidecar endpoint and to the local services this session uses. It admits no
  DNS and fails closed when it cannot enforce IPv6. This is the same
  `internet-proxy` firewall policy as firewall mode with `--internet-proxy`.
- **Proxy variables.** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy` and
  `https_proxy` are `http://host.docker.internal:3129`. `NO_PROXY` keeps the
  same local-service entries as in firewall mode.
- **Reaching the sidecar.** The sandbox reaches the published port through the
  runtime's local-service forwarding: the
  [localhost alias](apple-container-dns.md) on Apple `container`, and the
  `host.docker.internal` host alias on Docker Desktop and Podman machine.
  Concurrent sandboxes share the one published port.
- **Started on demand.** A sidecar-mode launch starts the sidecar if it is not
  running and ready, replacing a stopped one (for example after a reboot), and
  reuses it otherwise. Concurrent launches serialize on a lock, so only one
  starts it. Before the sandbox starts, the launch checks that the endpoint
  answers from the host. The sidecar keeps running when sessions end; stop it
  with `project-sandbox-egress down`.
- **Live changes.** Allowlist edits apply to the running sidecar without a
  restart (`squid -k reconfigure`; established tunnels survive). A launch also
  applies hand edits of the allowlist.
- **Restart-only changes.** The upstream, the Copilot profile, the port, the
  resource limits and a newer sidecar image need a restart. A launch warns and
  names each field, then keeps using the running sidecar, because an implicit
  restart would break every other sandbox. The port is the exception: the
  sandbox's firewall is built for the configured port, so a launch fails until
  you run `project-sandbox-egress restart`.
- **Limits.** The sidecar runs with `--memory` and `--cpus` from
  `[egress.sidecar]` (default `256M` and 1). On Docker and Podman its runtime
  log is capped at 10 MB.

Host state lives in `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/` (mode
0700). It holds:

- the rendered Squid config and the CA under `sidecar/`;
- a `state.json` cache;
- the Apple relay's lock, pid, port and log;
- each running session's sandbox inputs under `sessions/<id>/`.

Sidecar mode cannot be combined with `--no-firewall` or `--runtime chroot`. It
does not render or modify `.devcontainer/`, because devcontainers support
firewall egress only.

The first sidecar start builds a small Alpine/Squid image
(`project-sandbox-egress-sidecar:<hash>`). Switching a project between
`--egress firewall` and `--egress sidecar` rebuilds the sandbox image once,
because the firewall and entrypoint scripts differ between the modes.

## Policy

The policy is `$XDG_CONFIG_HOME/project-sandbox/egress.toml` (usually
`~/.config/project-sandbox/egress.toml`). It is the **only** policy: there is no
per-launch policy flag. In sidecar mode these flags are rejected, each with the
command or key to use instead: `--extra-domain`, `--internet-proxy`,
`--ca-cert`, `--allow-github` and `--copilot-host`. Without the file, built-in
defaults apply.

```toml
[egress]
allow = ["pypi.org", "files.pythonhosted.org", "*.npmjs.org"]

[egress.copilot]
enabled = false            # required for --agent copilot

[egress.upstream]          # optional parent proxy
url = "https://proxy.corp.example:8443"
ca_cert = ["~/corp-root-ca.pem"]   # relative paths: next to egress.toml

[egress.ca]
lifetime_days = 7          # 1 to 30

[egress.sidecar]
port = 3129                # published on 127.0.0.1 only; 1024 to 65535
memory = "256M"
cpus = 1
```

- **Effective allowlist:** `allow`, plus the provider hosts of every supported
  agent (Anthropic, OpenAI), which are always allowed, plus the Copilot profile
  when it is enabled. `project-sandbox-egress allow list` shows it.
- **Out of the sandbox's reach:** `egress.toml` and the state directory (which
  holds the CA key) must lie outside the project, the worktree, `.git`/`.jj`
  and every `--mount` source. This is checked after resolving symlinks.
  Mounting `~/.config` or `~/.local/state` into a sidecar sandbox is rejected.
- **Strict validation:** hostnames are validated strictly. IP literals, bare
  `*`, and wildcards over a single label such as `*.com` are rejected. Unknown
  keys are an error.
- **`--dry-run`** prints the policy, the effective allowlist, the CA plan and
  the published endpoint. A read-only `inspect` shows whether the sidecar would
  be started or reused and what differs. It writes no file, generates no key
  and creates nothing:

```bash
project-sandbox --dry-run --no-build --runtime docker --agent bash \
  --egress sidecar . python:3.12-slim
```

## Managing the sidecar

`project-sandbox-egress` manages the sidecar of one runtime. Every subcommand
takes `--runtime` (default: the runtime `project-sandbox` would select).

```bash
project-sandbox-egress up                      # start it now (launches do this too)
project-sandbox-egress status                  # state, endpoint, drift, CA, relay
project-sandbox-egress status --json
project-sandbox-egress allow list              # effective allowlist, providers marked
project-sandbox-egress allow add pypi.org '*.npmjs.org'
project-sandbox-egress allow remove pypi.org
project-sandbox-egress reload                  # apply a hand-edited egress.toml
project-sandbox-egress log --denied --since 10m
project-sandbox-egress log --denied --since 2026-11-06T10:42:13
project-sandbox-egress log --follow
project-sandbox-egress restart                 # apply restart-only changes
project-sandbox-egress restart --new-ca        # also rotate the CA
project-sandbox-egress down                    # stop it and delete its CA
```

- `allow add` and `allow remove` edit `egress.toml` and keep comments and other
  keys. They re-validate the whole file before replacing it, and apply the
  change to a running sidecar without a restart. An invalid edit leaves the
  file unchanged.
- `up` fails before creating anything when the configured port is taken on
  the host, and names `[egress.sidecar] port`.
- `down` and `restart` do not check for users. Running sidecar-mode sandboxes
  lose their connections; after a `restart` to a new port, relaunch them.
- Exit codes: 0 success, 1 operational failure, 2 usage error.

`project-sandbox doctor` shows a read-only status line per runtime (see
[doctor](doctor.md)).

## GitHub repository access is blocked

Sidecar mode does not admit GitHub repository hosts: `github.com`,
`api.github.com`, `uploads.github.com`, `codeload.github.com`,
`gist.github.com`, and the `raw`, `gist`, `media`, `objects`, `github-cloud`
and `release-assets` hosts under `githubusercontent.com`.

- They cannot be added through `allow` or `project-sandbox-egress allow add`,
  including by a covering wildcard such as `*.githubusercontent.com`.
- `--allow-github` and an `[egress.github]` table are rejected.
- `git clone`/`fetch`/`push` against GitHub, `gh`, archive downloads and REST
  calls fail inside the sandbox. That is the intended behaviour.

If a session needs GitHub, use firewall mode with `--allow-github`.
Per-repository read/write access for sidecar mode is on the
[roadmap](../ROADMAP.md#smart-github-repository-access-in-sidecar-egress-mode).

## Copilot profile

`[egress.copilot] enabled = true` admits only what the Copilot CLI needs.
`--agent copilot` requires it. When the policy or the running sidecar lacks it,
the launch fails before the sandbox starts and names the key. The profile was
measured with the `copilot-egress` e2e suite (see below):

- `api.individual.githubcopilot.com`, plus the `business` and `enterprise`
  counterparts. These are spliced, so Copilot's model traffic is never
  intercepted.
- `api.github.com`, **intercepted**. Only `/copilot_internal/…` (the
  entitlement check) passes; every other path gets a
  `project-sandbox-egress: path-not-allowed` 403. The CLI fails without this
  host, so it cannot simply be left out.

The CLI's update check (`/repos/github/copilot-cli/releases`) and its telemetry
(`telemetry.<plan>.githubcopilot.com`) are blocked on purpose. Copilot works
without them. `--copilot-host` tenants (`<tenant>.ghe.com`) are not supported
in sidecar mode.

### The sidecar CA

Interception needs a CA. It belongs to the sidecar, not to a session:

- EC P-256, `pathlen:0`, named `project-sandbox egress <runtime>`, valid for
  `[egress.ca] lifetime_days` (default 7, at most 30).
- Name-constrained to `github.com` and its subdomains. Squid mimics GitHub's
  `*.github.com` certificate names, so the constraint cannot be narrower.
- Reused across sidecar restarts while it has at least 2 days left and its
  constraints match the Copilot setting; otherwise `up` and `restart` generate
  a new one. `restart --new-ca` always does.
- The private key stays in
  `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/sidecar/` (mode 0600). It
  is mounted read-only into the sidecar only, and deleted by
  `project-sandbox-egress down`.
- Only the public certificate is mounted into the sandbox, at
  `/project-sandbox-egress/ca.pem`. The firewall step adds it to the system
  trust store at start-up, and the entrypoint exports `SSL_CERT_FILE`,
  `REQUESTS_CA_BUNDLE` and `NODE_USE_SYSTEM_CA=1`. It is never baked into an
  image, so rotating the CA does not rebuild the sandbox image.
- While interception is active, a launch warns when the CA expires within 2
  days (run `project-sandbox-egress restart`) and refuses to start a sandbox
  once it has expired.

Without the Copilot profile nothing is intercepted. The CA is then constrained
to the reserved name `invalid` and is not mounted into the sandbox at all.

**Clients with their own trust store** only matter for `api.github.com`, and in
sidecar mode only the Copilot CLI talks to that host. If you point another
client at it:

- **Java:** import `/project-sandbox-egress/ca.pem` into the keystore (`keytool
  -importcert -cacerts`).
- **Python with a pinned `certifi`** (some virtualenvs): set
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` (already exported), or use
  `pip-system-certs`.
- **Chromium/NSS:** add the certificate with `certutil` to `~/.pki/nssdb`.

## Local services

Local services work as in firewall mode, and the sidecar is not involved:

- A loopback `--agent-proxy` and Ollama (`--agent pi --pi-ollama`) are forwarded
  into the sandbox with the runtime's local-service strategy, at
  `host.docker.internal:<port>`. A service that listens only on the host's
  `127.0.0.1` works.
- A remote `--agent-proxy` is admitted as in firewall mode with
  `--internet-proxy`: a direct, port-scoped firewall destination. Model traffic
  does not pass through the sidecar.

## Parent proxy

With `[egress.upstream] url`, the sidecar sends every allowed request to that
proxy (Squid `cache_peer` with `never_direct`) and never goes direct. The
sidecar enforces the local allowlist in front of it. Changing the upstream
needs `project-sandbox-egress restart`.

```toml
[egress.upstream]
url = "https://proxy.corp.example:8443"
ca_cert = ["~/corp-root-ca.pem"]
```

- **Trust:** the sidecar uses `ca_cert` for an HTTPS parent and for
  re-originated `api.github.com` traffic. The file is also installed into each
  sandbox's system trust at container start, so spliced traffic that the
  corporate proxy intercepts validates. It is never baked into an image.
- **Hostname-only policy:** in parent mode the sidecar does not resolve names
  itself, because corporate networks often resolve external names only at the
  proxy. The private and metadata address checks are therefore skipped;
  IP-literal and port denials still apply. **The parent may reach intranet
  hosts, and the hostname allowlist is then the only thing between the agent
  and them.** Keep the allowlist tight.
- **Reaching the parent:** a parent on host loopback is reached at
  `host.docker.internal` on every runtime. Docker Desktop and Podman machine
  dial a remote parent directly. On Apple `container`, a remote parent is
  reached through a host-loopback relay that uses the host's VPN routes and
  DNS.
- **The Apple relay** is a detached host process owned by the sidecar, so it
  outlives the session that started it. It starts with the sidecar and stops
  with `down`. A launch restarts a relay that died. If its port was taken
  meanwhile, the relay moves to a free port and the sidecar follows without a
  restart.
- **Not supported:** proxy authentication. A 407 from the parent fails the
  request.

## Troubleshooting denials

Denied CONNECTs are **reset**: the client sees "connection reset" or "TLS
handshake failed", not an HTTP error. Squid would otherwise answer `200` and
then fail inside TLS. Plain-HTTP and intercepted requests get a 403 page that
reads `project-sandbox-egress: <cause>`.

At the end of each session, the CLI prints how many requests the sidecar
denied during the session, with the command that lists them. The count covers
**all** sandboxes and other clients of the sidecar (measured on the sidecar's
clock), not only this one:

```
Egress: the sidecar denied 3 requests during this session (all sandboxes on this sidecar).
  List them: project-sandbox-egress log --denied --since 2026-11-06T10:42:13
```

`log --denied` prints each denial with its time, target, cause and attribution
(local policy or upstream proxy). Its `client` address is the runtime's port
forwarder, not a sandbox, so denials cannot be attributed to individual
sandboxes; that is on the [roadmap](../ROADMAP.md).

| Cause | Meaning | Remedy |
|---|---|---|
| `host-not-allowed` | Not on the allowlist | `project-sandbox-egress allow add <host>` |
| `ip-literal` | Destination given as an IP address | Use a hostname |
| `port-not-allowed` | CONNECT to a port other than 443, or plain HTTP to a port other than 80 | — |
| `private-address` | Allowed name resolves to a private or metadata address (direct mode) | — |
| `sni-mismatch` | TLS SNI missing or different from the CONNECT host | Fix the client |
| `non-tls-tunnel` | Non-TLS bytes inside a CONNECT tunnel | — |
| `path-not-allowed` | Path outside the Copilot profile on `api.github.com` | Expected for repository APIs |
| `plain-http-intercepted` | `http://api.github.com` | Use HTTPS |
| `HTTP 403`/`407` (upstream proxy) | The parent proxy refused the request | Change the parent's policy |

**Known limitation:** an allowlisted name that resolves to a private address
(direct mode) gets `200` before the TLS failure, because that check waits for
DNS. No upstream connection is made, and the denial is logged as
`private-address`.

Other problems:

- **The port is taken.** `up` or the first launch fails and names
  `[egress.sidecar] port`. Stop the other listener or choose another port.
- **The launch says the sidecar publishes another port.** `egress.toml` sets a
  different port than the running sidecar uses. Run
  `project-sandbox-egress restart`.
- **The sidecar is not ready within 30 s.** The error includes the last 20 lines
  of the sidecar's log. Check that the image built, and on Podman that the
  machine is running.
- **The firewall reports the Internet proxy as unreachable on Apple
  `container`.** Set up the [localhost alias](apple-container-dns.md).
- **A launch warns about restart-only differences.** `egress.toml` or the
  installed version changed something the running sidecar picks up only on a
  restart. Run `project-sandbox-egress restart` when no sandbox needs it.

## Verification

Two opt-in e2e suites cover this mode. See the
[development guide](development.md).

- `scripts/run-e2e-tests.sh --only egress-sidecar` (not billable) runs real
  sessions and checks each policy probe both from inside the sandbox and in the
  sidecar log. A CONNECT denial passes only on zero bytes followed by a close
  or reset; a timeout or an unreachable sidecar fails. A second session turns
  the Copilot profile on and probes the intercepted `api.github.com`: an
  allowed path reaches GitHub, a denied path and plain HTTP get labelled 403
  pages, and a foreign or missing SNI and raw tunnels fail. The suite also
  covers parent chaining through an intercepting fixture
  proxy, a local service that listens only on host loopback, and the shared
  sidecar: two concurrent sandboxes, reuse across launches, a live `allow add`,
  and `down`.
- `scripts/run-e2e-tests.sh --only copilot-egress` (billable) captures the
  Copilot CLI's hosts and paths, verifies the Copilot profile, and checks that
  GitHub repository access stays blocked.
