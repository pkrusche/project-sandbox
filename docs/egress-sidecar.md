# Egress sidecar (`--egress sidecar`)

`--egress sidecar` routes all of a sandbox's Internet traffic through the
runtime's **global** [Squid](https://www.squid-cache.org/) sidecar. The sidecar
filters by **hostname**, not by IP address. The default `--egress firewall` mode
resolves each allowed domain once at start-up and pins the addresses, which
breaks when CDNs rotate addresses, admits unrelated hosts that share an IP, and
cannot express wildcards. The sidecar mode avoids those problems:

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

## Choosing an egress mode

| | Firewall (default) | Sidecar (`--egress sidecar`) | Your own proxy (`--internet-proxy`) |
|---|---|---|---|
| Policy | Domains pinned to IPs at start | Hostnames per request, wildcards | Whatever the proxy enforces |
| Where policy lives | Flags per launch (`--extra-domain`) | One global `egress.toml` | The proxy |
| Isolation between sandboxes | Each sandbox alone | Shared internal network, firewall-separated | Depends on the proxy |
| Live changes | Relaunch | `project-sandbox-egress allow add` | The proxy's own tooling |
| Extra resources | None | One sidecar per runtime | The proxy |
| GitHub | `--allow-github` | Blocked (Copilot profile only) | The proxy |

Use firewall mode for the tightest per-sandbox isolation, the sidecar for one
hostname policy shared by many sandboxes, and `--internet-proxy` when an
existing filtering proxy should decide on its own (see
[Internet proxy](internet-proxy.md)). The sidecar can also put a local
allowlist in front of such a proxy ([Parent proxy](#parent-proxy)).

## How it works

```
 sandbox A ──┐  sbx-egress (internal network)      ┌──────────────────────┐
             ├─ firewall: only the sidecar ────────┤ project-sandbox-egress├──▶ Internet
 sandbox B ──┘                                     │ (also on the runtime's│
                                                   │  default network)     │
                                                   └──────────────────────┘
```

Each runtime (Apple `container`, Docker, Podman) has **one** sidecar container,
`project-sandbox-egress`, on **one** internal network, `sbx-egress`, which has
no route to the Internet. Every sidecar-mode sandbox of that runtime joins that
network only.

- **Started on demand.** A sidecar-mode launch starts the sidecar if it is not
  running and ready, replacing a stopped one (for example after a reboot), and
  reuses it otherwise. Concurrent launches serialize on a lock, so only one
  starts it. The sidecar keeps running when sessions end; stop it with
  `project-sandbox-egress down`.
- **Per-session firewall.** Each sandbox's firewall drops everything except TCP
  to the sidecar's proxy port and to the local-service ports that *this*
  session uses, and allows no DNS. `HTTP_PROXY`/`HTTPS_PROXY` point at
  `http://egress-proxy:3128`.
- **Shared, not merged.** Sandboxes share the network but cannot open
  connections to each other; see [Security](security.md#egress-sidecar).
- **Live changes.** Allowlist and forwarder edits apply to the running sidecar
  without a restart (`squid -k reconfigure`, established tunnels survive). A
  launch also applies hand edits of the allowlist. Changes that need a restart
  (upstream, Copilot profile, resources, a newer sidecar image) produce a
  warning that names each field, and the launch keeps using the running
  sidecar: an implicit restart would break every other sandbox.
- **Limits.** The sidecar runs with `--memory` and `--cpus` from
  `[egress.sidecar]` (default `256M` and 1). On Docker and Podman its runtime
  log is capped at 10 MB.

Host state lives in `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/`
(mode 0700): the rendered Squid config and the CA under `sidecar/`, a
`state.json` cache, the Apple relay's lock, pid and log, and each running
session's sandbox inputs under `sessions/<id>/`.

Works on Apple `container` (1.4.1 or later), Docker and Podman. Sidecar mode
cannot be combined with `--no-firewall` or `--runtime chroot`. It does not
render or modify `.devcontainer/`, because devcontainers support firewall egress
only.

The first sidecar start builds a small Alpine/Squid image
(`project-sandbox-egress-sidecar:<hash>`). Switching a project between
`--egress firewall` and `--egress sidecar` rebuilds the sandbox image once,
because the firewall and entrypoint scripts differ between the modes.

Resources left by per-session sidecars of earlier versions
(`sbx-egress-<id>`, `project-sandbox-egress-<id>`) are removed by the next
launch, `project-sandbox-egress up` or `down`. `project-sandbox doctor` lists
them.

## Policy

The policy is `$XDG_CONFIG_HOME/project-sandbox/egress.toml` (usually
`~/.config/project-sandbox/egress.toml`). It is the **only** policy: there is no
per-launch policy flag, and `--extra-domain`, `--internet-proxy` and
`--ca-cert` are rejected in sidecar mode with the command or key to use instead.
Without the file, built-in defaults apply.

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
memory = "256M"
cpus = 1
forward_ports = [8787, 11434]      # local services the sidecar forwards
```

- **Effective allowlist:** `allow` plus the provider hosts of every supported
  agent (Anthropic, OpenAI), which are always allowed, plus the Copilot profile
  when it is enabled. `project-sandbox-egress allow list` shows it. For tighter
  isolation per sandbox, use firewall mode.
- **Out of the sandbox's reach:** `egress.toml` and the state directory (which
  holds the CA key) must lie outside the project, the worktree, `.git`/`.jj`
  and every `--mount` source, checked after resolving symlinks. Mounting
  `~/.config` or `~/.local/state` into a sidecar sandbox is rejected.
- **Strict validation:** hostnames are validated strictly. IP literals, bare
  `*`, and wildcards over a single label such as `*.com` are rejected. Unknown
  keys are an error.
- **`--dry-run`** prints the policy, the effective allowlist, the CA plan and,
  from a read-only `inspect`, whether the sidecar would be started or reused
  and what differs. It writes no file, generates no key and creates nothing:

```bash
project-sandbox --dry-run --no-build --runtime docker --agent bash \
  --egress sidecar . python:3.12-slim
```

## Managing the sidecar

`project-sandbox-egress` manages the sidecar of one runtime. Every subcommand
takes `--runtime` (default: the runtime `project-sandbox` would select).

```bash
project-sandbox-egress up                      # start it now (launches do this too)
project-sandbox-egress status                  # state, drift, CA, relay, attached sandboxes
project-sandbox-egress status --json
project-sandbox-egress allow list              # effective allowlist, providers marked
project-sandbox-egress allow add pypi.org '*.npmjs.org'
project-sandbox-egress allow remove pypi.org
project-sandbox-egress forward add 8787        # forward a host-local service
project-sandbox-egress forward list            # configured and running forwarders
project-sandbox-egress reload                  # apply a hand-edited egress.toml
project-sandbox-egress log --denied --since 10m
project-sandbox-egress log --follow
project-sandbox-egress restart                 # apply restart-only changes
project-sandbox-egress restart --new-ca        # also rotate the CA
project-sandbox-egress down                    # stop it and delete its CA
```

- `allow` and `forward` edit `egress.toml` (comments and other keys are kept),
  re-validate the whole file before replacing it, and apply the change to a
  running sidecar without a restart. An invalid edit leaves the file unchanged.
- `down` and `restart` do not check for users. They print the attached
  sandboxes first; those lose egress and need to be relaunched after a
  `restart`. `down` leaves `sbx-egress` in place while sandboxes are attached.
- Exit codes: 0 success, 1 operational failure, 2 usage error.

## GitHub repository access is blocked

Sidecar mode does not admit GitHub repository hosts: `github.com`,
`api.github.com`, `codeload.github.com`, `raw.githubusercontent.com`, gists,
release-asset and LFS hosts, and the rest of
`egress_policy.GITHUB_REPOSITORY_HOSTS`.

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
`--agent copilot` requires it, and fails before the sandbox starts (naming the
key) when the policy or the running sidecar lacks it. This was measured with the `copilot-egress` e2e suite (see
below):

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
in sidecar mode yet.

### The sidecar CA

Interception needs a CA. It belongs to the sidecar, not to a session:

- EC P-256, `pathlen:0`, named `project-sandbox egress <runtime>`, valid for
  `[egress.ca] lifetime_days` (default 7, at most 30).
- Name-constrained to `github.com` and its subdomains. Squid mimics GitHub's
  `*.github.com` certificate names, so the constraint cannot be narrower.
- Reused across sidecar restarts while it has at least 2 days left and its
  constraints match the Copilot setting; otherwise `up`/`restart` generate a
  new one. `restart --new-ca` always does.
- The private key stays in `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/sidecar/`
  (mode 0600). It is mounted read-only into the sidecar only, and deleted by
  `project-sandbox-egress down`.
- Only the public certificate is mounted into the sandbox. The firewall step
  adds it to the system trust store at start-up, and the entrypoint exports
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `NODE_USE_SYSTEM_CA=1`. It is never
  baked into an image.
- While interception is active, a launch warns when the CA expires within 2
  days (run `project-sandbox-egress restart`) and refuses to start a sandbox
  once it has expired.

Without the Copilot profile nothing is intercepted. The CA is then constrained
to the reserved name `invalid` and is not mounted into the sandbox at all.

**Clients with their own trust store** only matter for `api.github.com`, and
in sidecar mode only the Copilot CLI talks to that host. If you point another
client at it:

- **Java:** import `/project-sandbox-egress/ca.pem` into the keystore (`keytool
  -importcert -cacerts`).
- **Python with a pinned `certifi`** (some virtualenvs): set
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` (already exported), or use
  `pip-system-certs`.
- **Chromium/NSS:** add the certificate with `certutil` to `~/.pki/nssdb`.

## Local services

The sandbox cannot reach the host from its internal network. The sidecar
therefore forwards host-local services: a loopback `--agent-proxy`, and Ollama
with `--agent pi --pi-ollama`. In the sandbox, `host.docker.internal` resolves
to the sidecar, so client URLs and `NO_PROXY` stay as in firewall mode.

- **Global forwarders, per-session firewall.** The sidecar forwards every port
  in `[egress.sidecar] forward_ports`; each sandbox's firewall opens only the
  ones its own session uses. A launch that needs a port the sidecar does not
  forward fails and names `project-sandbox-egress forward add <port>`.
- The forwarders accept connections only from the internal network. Ports below
  1024 and the proxy port are refused.
- **Runtimes.** Forwarding needs a runtime-provided `host.docker.internal` that
  reaches the host's loopback: Apple `container` with the
  [localhost alias](apple-container-dns.md), Docker Desktop and Podman machine.
  On Docker Engine and rootless Podman with pasta, a sidecar with forwarders is
  rejected before anything is created; without forwarders it works there.

A remote `--agent-proxy` is not supported in sidecar mode yet.

## Parent proxy

With `[egress.upstream] url`, the sidecar sends every allowed request to that
proxy (Squid `cache_peer` with `never_direct`). The sidecar enforces the local
allowlist in front of it. Changing the upstream needs `project-sandbox-egress
restart`.

```toml
[egress.upstream]
url = "https://proxy.corp.example:8443"
ca_cert = ["~/corp-root-ca.pem"]
```

- **Trust:** `ca_cert` is given to the sidecar, which uses it for an HTTPS
  parent and for re-originated `api.github.com` traffic. It is also installed
  into each sandbox's system trust at container start, so spliced traffic that
  the corporate proxy intercepts validates. It is never baked into an image.
- **Hostname-only policy:** in parent mode the sidecar does not resolve names
  itself, because corporate networks often resolve external names only at the
  proxy. The private/metadata address checks are therefore skipped; IP-literal
  and port denials still apply. **The parent may reach intranet hosts, and the
  hostname allowlist is then the only thing between the agent and them.** Keep
  the allowlist tight.
- **Apple `container`:** a remote parent is reached through a host-loopback relay
  that uses the host's VPN routes and DNS. The relay is a detached host process
  owned by the sidecar (it outlives the session that started it), started with
  the sidecar and stopped by `down`. A launch restarts a relay that died; if its
  port was taken meanwhile, it moves to a free port and the sidecar follows
  without a restart. A loopback parent is reached through `host.docker.internal`
  without a relay; Docker and Podman dial the parent directly.
- **Not supported:** proxy authentication. A 407 from the parent fails the
  request.

## Troubleshooting denials

Denied CONNECTs are **reset**: the client sees "connection reset" or "TLS
handshake failed", not an HTTP error. Squid would otherwise answer `200` and
then fail inside TLS. Plain-HTTP and intercepted requests get a 403 page that
reads `project-sandbox-egress: <cause>`.

At the end of each session, the CLI prints how many requests the sidecar
denied during the session, across **all** sandboxes that share it (measured on
the sidecar's clock), with the command that lists them:

```
Egress: the sidecar denied 3 requests during this session (all sandboxes on this sidecar).
  List them: project-sandbox-egress log --denied --since 2026-11-06T10:42:13
```

`log --denied` prints each denial with its time, client IP (the sandbox's
address on `sbx-egress`), target and cause. Attributing denials to individual
sandboxes is on the [roadmap](../ROADMAP.md).

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

- **The sidecar is not ready within 30 s.** The error includes the last 20 lines
  of the sidecar's log. Check that the image built, and on Podman that the
  machine is running.
- **A launch warns about restart-only differences.** `egress.toml` or the
  installed version changed something the running sidecar can only pick up on
  a restart. Run `project-sandbox-egress restart` when no sandbox needs it.
- **Sandboxes lost egress after a restart.** Expected: the sidecar's internal
  address can change, so relaunch them.

## Verification

Two opt-in e2e suites cover this mode. See the
[development guide](development.md).

- `scripts/run-e2e-tests.sh --only egress-sidecar` (not billable) runs real
  sessions and checks each policy probe both from inside the sandbox and in the
  sidecar log. It also covers parent chaining through an intercepting fixture
  proxy, local-service forwarding, and the shared sidecar: two concurrent
  sandboxes, reuse across launches, a live `allow add`, and `down`.
- `scripts/run-e2e-tests.sh --only copilot-egress` (billable) captures the
  Copilot CLI's hosts and paths, verifies the Copilot profile, and checks that
  GitHub repository access stays blocked.
