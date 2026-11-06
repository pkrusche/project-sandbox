## Context

project-sandbox has two egress modes today:

```
 firewall (default)                      firewall + --internet-proxy
 sandbox: iptables + ipset               sandbox: iptables admits only proxy IP:port
 dig domain → pin IPs once               HTTP(S)_PROXY → user-managed proxy
```

The IP-pinning design filters by address, not by hostname. Policy is coarse (`--allow-github` admits all of GitHub) and breaks when DNS rotates. `--internet-proxy` leaves lifecycle and policy to an external proxy. On Apple `container`, it reaches remote proxies through a host relay whose port is derived from the proxy URL because the firewall is baked into the image. As a result, concurrent sessions that use the same proxy conflict.

[internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally) already renders a default-deny Squid config from a hostname allowlist, with opt-in `ssl_bump` and a signing CA. Its `docs/findings.md` lab results for Squid:

- `connect-sni-mismatch` failed with interception both on and off.
- `connect-raw-tunnel` failed with interception off.
- With interception on, Squid answered `200 Connection established` to the metadata address before denying it.
- SSRF ranges, DNS rebinding and mixed public/private answers were denied.

Its shipped config drops WebSocket upgrades.

Apple `container` 1.4.1 supports `container network create --internal` ("host-only") and `container run --network <name>`.

## Goals / Non-Goals

**Goals:**
- Filter outbound traffic by hostname, re-resolving on every request, with wildcards. No IP pinning in the new mode.
- The sandbox has no route to the Internet except through its own sidecar, on Docker, Podman and Apple `container`.
- Block GitHub repository access (git, REST, raw, archives, gists) while the Copilot CLI keeps working. Per-repository access is on the roadmap (ROADMAP.md, "Smart GitHub repository access").
- Use TLS interception only where path rules need it, which is at most Copilot's token exchange on `api.github.com`. The CA is per session, name-constrained, and its key never enters the sandbox.
- Optionally chain to a corporate parent proxy that intercepts TLS with its own CA, including on Apple through VPN routes.
- The agent cannot alter the policy that governs it, now or in later sessions.

**Non-Goals:**
- Upstream proxy authentication. A 407 fails the request.
- Credential injection by the sidecar. Recorded in TODO.md.
- Request-body inspection, DLP, ICAP.
- Per-repository GitHub access (read/write rules, LFS, release assets). Moved to ROADMAP.md, together with the URL-shape work that was task 1.2.
- Transparent interception. Clients that ignore proxy variables fail closed.
- Sidecar mode for devcontainers or chroot. A sidecar-mode launch does not render or modify `.devcontainer/`.
- Changing the default mode or firewall-mode behaviour.
- Docker Compose. Lifecycle stays in the CLI.

## Decisions

### 1. Topology: per-session internal network with a dual-homed sidecar

```
            ┌─ sbx-<sid>-int (internal / host-only) ─┐
 sandbox ───┤                                         ├─── squid sidecar ──▶ egress network ──▶ Internet
 (internal  │  iptables: only sidecar IP:3128         │    (also on the       (default runtime
  only)     │  + local-service ports on the sidecar   │     egress network)    network)
            └─────────────────────────────────────────┘
```

- **Docker/Podman:** `network create --internal sbx-<sid>-int`. Start the sidecar on the default network, then `network connect` it to the internal network. Start the sandbox with `--network sbx-<sid>-int` only.
- **Apple `container`:** `network create --internal sbx-<sid>-int`. The sidecar is started with `--network default --network sbx-<sid>-int`, which spike 0.1 confirmed. The sandbox is started with `--network sbx-<sid>-int` only.
- **Host-only is not isolated.** On Apple, the sandbox can still reach the host's vmnet address. In Docker Desktop, the host may be reachable through the internal bridge gateway. So the sandbox firewall stays mandatory and uses a new `sidecar` policy:
  - Default DROP.
  - ACCEPT TCP to the sidecar IP:3128.
  - ACCEPT TCP to the sidecar IP on each enabled local-service port (agent proxy, Ollama). These are forwarded by the sidecar; see "Local services" below.
  - No other destination, the host included. DNS is not allowed. The sandbox does not need DNS for Internet hosts, and the entrypoint's root phase writes `/etc/hosts` entries for `egress-proxy` and `host.docker.internal`, both pointing at the sidecar's internal IP.
- **Local services are forwarded by the sidecar on every runtime.** This was decided after spike 0.3. On Docker and Podman, the Mac host is unreachable from an internal network. On Apple, the host is reachable but `host.docker.internal` does not resolve on the host-only network. One mechanism therefore serves all three runtimes:
  - For each enabled local service (agent proxy, Ollama), the sidecar runs `socat TCP-LISTEN:<port>,fork,reuseaddr,range=<internal subnet> TCP:host.docker.internal:<port>`, as an unprivileged user.
  - `range=` limits accepted peers to the session's internal subnet, so other containers on the default network cannot use the sidecar as a path to host services.
  - The sidecar reaches the host as a normal default-network container would. Apple: `host.docker.internal` → 203.0.113.113 → host loopback (spike 0.1). Docker Desktop: `host.docker.internal`. Podman: the existing `local_service_network` plan for the runtime.
  - Local-service ports below 1024 are rejected in sidecar mode.
  - The sandbox maps `host.docker.internal` to the sidecar's internal IP. Client URLs (`OLLAMA_BASE_URL`, the agent proxy URL) and the `NO_PROXY` bypass entries therefore stay as in firewall mode.
  - The local `--internet-proxy` service is not forwarded to the sandbox in sidecar mode, because the sidecar uses it directly as its parent.
  - The sidecar's internal IP is read with `inspect` after it joins the internal network. On Apple that is the `run`; on Docker and Podman it is the `network connect`. The CLI then passes the IP to the sandbox (`SBX_EGRESS_SIDECAR_IP`). The entrypoint uses it for both `/etc/hosts` and the firewall rule, so all runtimes share one mechanism, and runtime `--add-host` and aliases are not needed.
- **Spike 0.1 results (Apple `container` 1.4.1, macOS 27.0.1 arm64, 2026-11-02).** The primary topology works, so the fallback is not needed.

  Commands (image `alpine:3.22.1`; the sidecar ran a `nc -l -p 3128` responder):

  ```bash
  container network create --internal --label project-sandbox.egress.session=spike01 sbxspike-int
  container run -d --name sbxspike-sidecar --label project-sandbox.egress.session=spike01 \
    --network default --network sbxspike-int alpine:3.22.1 sh -c '<nc responder on 3128>'
  container run -d --name sbxspike-sandbox --network sbxspike-int alpine:3.22.1 sleep 600
  container exec sbxspike-sandbox …   # probes below (wget -T 4 / nc -z -w 4)
  container rm -f sbxspike-sandbox sbxspike-sidecar && container network rm sbxspike-int
  ```

  | Check | Result |
  |---|---|
  | `network create --internal` | OK. `mode: hostOnly`, subnet 192.168.128.0/24, gateway .1. Labels are stored and shown by `network inspect`. |
  | Two `--network` flags on one `run` | OK. The sidecar got `eth0` 192.168.64.35 (default, default route) and `eth1` 192.168.128.2 (internal). |
  | Sandbox → public IPv4 (1.1.1.1:80/443) | Timed out. Its default route is the host-only gateway, which does not forward. |
  | Sandbox → public IPv6 | No route. Only the ULA and link-local prefixes are present. |
  | Sandbox DNS (`nameserver 192.168.128.1`) | Times out. There is no DNS on a host-only network, so neither public names nor `host.docker.internal` resolve. |
  | Sandbox → sidecar internal IP:3128 | OK. |
  | Sandbox → sidecar default-network IP | Timed out. |
  | Sandbox → host-only gateway 192.168.128.1 | **Reachable.** Host wildcard listeners (`*:7000`) accept connections, and closed ports refuse. |
  | Sandbox → 203.0.113.113 (`container system dns … --localhost` redirect) | **Reachable, including host loopback-only listeners** (`127.0.0.1:44438`). |
  | Sandbox → default gateway 192.168.64.1 | Timed out. |
  | Sidecar → Internet, DNS | OK (`nc -z 1.1.1.1 443`, `nslookup example.com` through 192.168.64.1). |
  | Sidecar `host.docker.internal` | Resolves to 203.0.113.113 (the existing Apple localhost redirect) and reaches host loopback listeners. So the parent-proxy host relay is reachable from the sidecar as in firewall mode. |

  Consequences for implementation:
  - **The sandbox firewall is mandatory on Apple.** From the internal network, the sandbox can reach every host wildcard listener through the gateway and every host loopback listener through 203.0.113.113. The `sidecar` firewall policy must DROP by default and admit only the sidecar's internal IP:3128 and the enabled local-service destinations. The local services (agent gateway, Ollama) remain reachable through 203.0.113.113 from the internal network, so they keep working in sidecar mode.
  - **There is no `--add-host` on Apple `container run`** (only `--dns*` flags), and the host-only network serves no DNS. The `egress-proxy` name therefore cannot come from runtime flags. The CLI reads the sidecar's internal address from `container inspect` (`networks[] | select(.network == "<int>") | .ipv4Address`) after the sidecar starts. It passes the address to the sandbox, which uses it for the firewall rule and writes `egress-proxy` into `/etc/hosts` during the root phase of the entrypoint. Docker and Podman keep `--add-host`.
  - Addresses are allocated dynamically per network. A fixed `--subnet` is not needed.
- **Spike 0.3 results (Docker Desktop 29.9.2 and Podman 6.0.1 `applehv` machine, macOS arm64, 2026-11-04).** The same topology works on both.

  Commands (`R` = `docker` or `podman`; image `alpine:3.22.1`; the sidecar ran an `nc -l -p 3128` responder). No host listeners were started. Host reachability was inferred from *refused* (reachable) versus *unreachable* or *timed out* on a closed port.

  ```bash
  $R network create --internal --label project-sandbox.egress.session=spike03 sbxspike-int
  $R run -d --name sbxspike-sidecar --label … alpine:3.22.1 sh -c '<nc responder on 3128>'   # default network
  $R network connect --alias egress-proxy sbxspike-int sbxspike-sidecar
  $R run -d --name sbxspike-sandbox --label … --network sbxspike-int alpine:3.22.1 sleep 600
  $R exec sbxspike-sandbox …   # probes (wget -T 4 / nc -z -w 4 / nslookup)
  $R rm -f sbxspike-sandbox sbxspike-sidecar && $R network rm sbxspike-int
  ```

  | Check | Docker Desktop | Podman |
  |---|---|---|
  | `network create --internal`, `network connect --alias` | OK (172.18.0.0/16) | OK (10.89.0.0/24) |
  | Sandbox routes | Only the on-link internal subnet. No default route. | Same |
  | Sandbox → public IPv4 / IPv6 | Blocked | Blocked |
  | Sandbox DNS for public names and `host.docker.internal` | Blocked (embedded DNS 127.0.0.11 does not forward on internal networks) | NXDOMAIN (aardvark-dns does not forward on internal networks) |
  | Sandbox resolves `egress-proxy` alias | Yes, via embedded DNS | Yes, via an `/etc/hosts` entry Podman writes |
  | Sandbox → sidecar internal IP:3128 | OK | OK |
  | Sandbox → sidecar default-network IP | Unreachable | Unreachable |
  | Sandbox → internal gateway (x.x.x.1) | **Reachable** (refused). This is the runtime VM's bridge, not the Mac. | **Reachable** (refused), the Podman VM |
  | Sandbox → Mac host (`host.docker.internal`, 192.168.65.254, `host.containers.internal`/192.168.127.254) | Name does not resolve, IP unreachable | Network unreachable |
  | Sidecar → Internet | OK, DNS OK | TCP OK. **DNS fails**: the internal network's aardvark (10.89.0.1) is listed first in the sidecar's `resolv.conf` and answers NXDOMAIN for public names. |
  | Podman with `network create --internal --disable-dns` | n/a | Sidecar DNS OK (only the machine's resolvers are listed). The sandbox still has no route or DNS, but the `egress-proxy` alias is **not** written to `/etc/hosts`. |

  Consequences for implementation:
  - **Docker:** the embedded DNS would serve an `--alias`, but names come from the entrypoint's `/etc/hosts` on all runtimes (see "Local services"), so no alias is needed.
  - **Podman:** create the internal network with `--disable-dns`, so that the sidecar's resolver order is not poisoned. The missing alias does not matter, because the entrypoint writes `/etc/hosts`.
  - **The sandbox firewall stays mandatory on Docker and Podman too.** The internal gateway answers, so anything listening on the runtime VM's wildcard address is reachable.
  - **Local services are not reachable directly on Docker or Podman.** Unlike Apple, the Mac host is unreachable from an internal network. This is resolved by sidecar forwarding (see "Local services" above).
- **Sandbox side (group 7):**
  - **Per-session inputs travel by file, not environment.** The entrypoint reaches root only through `sudo -n project-sandbox-init-firewall`, and sudo's `env_reset` would strip variables. So `egress_sidecar.session()` writes `<session>/sandbox/egress.conf` (`sidecar_ip=…`, `forward_ports=…`) and, with interception, a copy of `ca.pem`. Only that `sandbox/` subdirectory is mounted read-only at `/project-sandbox-egress`; the key one level up never is.
  - **The firewall script** (`init-firewall-sidecar.sh.j2`, firewall policy `sidecar`, rendered as `init-firewall.sh` in sidecar mode only) works as follows:
    - It parses `egress.conf` strictly (known keys only, IPv4 and port validation, never sourced).
    - It rewrites `/etc/hosts` in place, so `egress-proxy` and `host.docker.internal` point at the sidecar. Docker bind-mounts `/etc/hosts`, so `sed -i` fails there.
    - It installs `ca.pem` into the system store when present.
    - The rules: DROP by default, ACCEPT to the sidecar IP on 3128 and on each forward port, IPv6 dropped (or disabled), no DNS, no ipset.
    - It verifies the sidecar is reachable through the final rules.
    - The script holds no session data, so the sandbox image stays cacheable across sessions. Sidecar mode does change `init-firewall.sh` and `entrypoint.sh`, so switching between modes rebuilds the image.
  - **The entrypoint** gets a sidecar-only block (`render_entrypoint(egress_sidecar=True)`) that exports `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `NODE_USE_SYSTEM_CA` when `ca.pem` is mounted. Firewall-mode output is byte-identical to before.
  - **Proxy environment:** `internet_proxy.sidecar_environment()` sets `HTTP(S)_PROXY=http://egress-proxy:3128` with the same `NO_PROXY` entries as firewall mode.
  - **Live check** (Apple `container` and Docker; a Debian test image with the rendered script, on the internal network with `NET_ADMIN`; a real sidecar session):
    - The allowed host returned 200 through the sidecar.
    - `api.github.com/copilot_internal/user` returned 401 from GitHub through interception, so the runtime CA install worked.
    - `/repos/…` got the sidecar's 403.
    - The unlisted host, direct egress, the gateway, other sidecar ports and DNS were all blocked.
    - Forwarding to an actual host service was not exercised (no host listener was started); the e2e suite covers it.
- **Alternatives considered:**
  - A shared network namespace (`--network container:`) is impossible on Apple, where each container is a VM.
  - Publishing the sidecar on host loopback reproduces today's port-conflict and shared-listener problems and gives no routing isolation.

### 2. Squid policy pipeline

```
 CONNECT host:port ─▶ http_access: port == 443?                  no ─▶ TCP_RESET (no 200)
                      host is an IP literal?                     yes ─▶ TCP_RESET
                      host ∈ allow (dstdomain)?                   no ─▶ TCP_RESET
                      [direct mode] dst ∈ metadata/private?      yes ─▶ TCP_RESET
                      ─▶ 200 Connection established
 ssl_bump step1  peek
 ssl_bump step2  SNI present AND SNI == CONNECT host?  (sni-match helper)  no ─▶ terminate
                 host ∈ bump-set?                                         yes ─▶ bump ─▶ http_access path rules (labelled 403)
                 otherwise                                                    ─▶ splice
 non-TLS bytes after 200 ─▶ peek fails ─▶ on_unsupported_protocol respond ─▶ closed, no upstream connection
```

- **CONNECT-level denials use `deny_info TCP_RESET`.** On an `ssl-bump` port, Squid 6.12 bumps every denied CONNECT (except a 407) in order to serve the error page over TLS. See `sslBumpAccessCheck()` in `src/client_side_request.cc` ("Force bump action on error"). The client then receives `200`, followed by a TLS session with a minted certificate, which is the upstream "200 then deny" behaviour. Spike 0.2 confirmed this happens even when the denial is in `http_access`. With `TCP_RESET` on those denials, the client receives zero bytes and then a reset: no `200`, no minted certificate, no upstream connection.
  - **Cost:** the client sees a reset, not a labelled page.
  - **Where the cause goes instead:** each cause is recorded with a `note sbx_cause <cause> <acls>` annotation (logged as `%{sbx_cause}note`), and the session-end summary reads it.
  - **Log status:** Squid still logs the status as `TCP_DENIED/200`, with `err_code` `TCP_RESET`, so the log parser must key on `err_code`.
  - **Note ACLs:** each note must use the cause's real condition ACLs, not an always-true ACL, or the first note labels every transaction.
- **SNI equality uses an external ACL helper.** When SNI is absent, `ssl::server_name --client-requested` falls back to the CONNECT host (see `ACLServerNameStrategy::match` in `src/acl/ServerName.cc`), so no built-in ACL can detect a missing SNI. Per-entry `dstdomain` + `ssl::server_name` pairs also cannot express equality for wildcard entries. Instead, `external_acl_type sni_match ttl=60 negative_ttl=60 %ssl::>sni %>rd /usr/local/libexec/sni-match` runs a few lines of shell in the image. It returns OK only when the SNI is present and equals the CONNECT host, compared case-insensitively. Because the CONNECT host has already passed the allowlist, a matching SNI is allowlisted as well. This closes `connect-sni-mismatch` and the no-SNI case.
- **Raw tunnels:** `on_unsupported_protocol respond all` (no `tunnel` action). Non-TLS bytes after an allowed CONNECT fail the peek, are logged as `peek ERR_PROTOCOL_UNKNOWN`, and the connection closes without an upstream connection. This closes `connect-raw-tunnel`.
- **Plain HTTP (port 80)** is evaluated with the same `dstdomain` allowlist (`proto HTTP` distinguishes it from bumped requests, which are `proto HTTPS`). Bump-set hosts reached over plain HTTP are denied with a labelled page.
- **An `ssl-bump` port needs a signing certificate even for peek/splice only.** Squid 6.12 crashes at startup (`FATAL: Received Segment Violation`) on `http_port … ssl-bump` without `tls-cert`. See Decision 3 for the consequence.
- The rest follows the upstream template: caching off, logs to stdout/stderr, `host_verify_strict on`, no `dns_defnames`, and distinct `deny_info` pages for denials of plain HTTP and bumped requests, prefixed `project-sandbox-egress:`.
- **Rendered config (task 5.2, `squid_config.py`, `templates/egress-sidecar/squid.conf.j2`).**
  - Each cause has an always-true marker ACL, because `deny_info` binds to the last ACL on a deny line.
  - `note sbx_cause` lines are ordered port, IP literal, private address, host, then the intercepted-host causes; the first match labels the transaction.
  - Error pages live in `error_directory /etc/squid/sbx-errors`, which holds Squid's stock templates plus `ERR_SBX_*` pages reading `project-sandbox-egress: <cause>`.
  - The access log uses a `SBX`-prefixed `logformat` (`… sni= bump= err= cause= peer=`) for the session summary.
  - `forwarded_for delete` keeps the sandbox address out of upstream requests. `via off` was dropped, because Squid warns that HTTP requires Via, and the header only names the sidecar.
  - `::ffff:0:0/96` is not in `private_dst`: Squid stores IPv4 as mapped addresses, so that range means all of IPv4.
- **Live check of the rendered config** (Apple `container`, sidecar image from task 5.1, probes from a client container):
  - **Resets, no `200`:** unlisted host (`host-not-allowed`), `1.1.1.1` (`ip-literal`), port 8443 (`port-not-allowed`).
  - **Labelled 403s:** plain HTTP to an unlisted host, to port 8080, to a private-resolving host, and to `api.github.com`.
  - **Intercepted `api.github.com`:** `/copilot_internal/user` reached GitHub (401 without a token); `/repos/…` and `POST /graphql` got `path-not-allowed` 403s.
  - **TLS level:** SNI mismatch and missing SNI were terminated; a raw tunnel closed after `200` with `peek ERR_PROTOCOL_UNKNOWN`.
- **Known limitation: resolved-address denials answer `200` first.** In direct mode, an allowlisted name that resolves to a private address (`127.0.0.1.nip.io`) is denied with the right cause, and no upstream connection is made. But because the `dst` check waits for a DNS lookup, Squid has already answered `200`, and the client sees a TLS failure on a certificate outside the CA's name constraint instead of a reset. IP-literal metadata requests are still reset before `200`. The e2e suite grades this case on "no upstream connection plus a logged cause".
- WebSocket upgrades are not enabled, matching upstream. Agent model traffic goes to the gateway or provider and does not need them through the proxy. This is revisited only if a required client breaks.
- The config is rendered from `squid.conf.j2`. Every hostname and repository string is validated against strict patterns before rendering, because it is inserted into config.

### 3. Selective interception and the per-session CA

- **Bump-set:** empty, or `api.github.com` alone when the Copilot profile is on and `api.github.com` is handled in `paths` mode (Decision 5). Nothing else is ever intercepted, so ordinary clients (pip, npm, git to allowed hosts) never see the session CA.
- **A CA exists in every sidecar session.** Squid's `ssl-bump` port needs a signing certificate even for peek/splice only (Decision 2).
  - Name constraints: the bump-set hosts' registrable domain (`permitted;DNS:github.com`, which covers `github.com` and its subdomains). It cannot be narrower, because Squid's `generate-host-certificates` mimics the origin certificate's names. For `api.github.com`, GitHub serves `CN=*.github.com` with SANs `*.github.com, github.com`. A constraint of only `api.github.com` made every intercepted request fail with `permitted subtree violation` (found with the live config, task 5.2). With an empty bump-set, the single constraint is `permitted;DNS:invalid` (a reserved TLD, RFC 6761).
  - With an empty bump-set the CA is not mounted into the sandbox, so no client trusts it, and any certificate it could mint would violate its own constraint.
- **CA generation** happens on the host. The CA is EC P-256, valid for the session plus a small margin and capped at 7 days, with `BasicConstraints(ca=True, path_length=0)`, `KeyUsage(keyCertSign, cRLSign)` and critical `NameConstraints` as above.
  - Clients that ignore name constraints still get correct behaviour, because the sidecar mints certificates only for bump-set hosts (CONNECT denials are resets, Decision 2). The constraint limits a leaked key.
  - Implemented in `session_ca.py` with `cryptography` (pinned 50.0.2, a new runtime dependency). The private key is unencrypted PKCS#8 PEM, which Squid reads directly.
- **Key handling:**
  - The files live in a session directory `$XDG_STATE_HOME/project-sandbox/egress/<sid>/` (next to the `sessions/` records): directory 0700, `ca-key.pem` 0600, `ca.pem` 0644. They are written with `O_EXCL | O_NOFOLLOW` and never overwritten.
  - The key is mounted read-only into the sidecar only. The sidecar entrypoint copies it to tmpfs for `security_file_certgen` and drops privileges.
  - The directory is deleted at teardown (`session_directory()` context manager, which also covers the exception path), and stale ones are removed on the next launch.
  - **Liveness:** the running session holds an `flock` on `<dir>/.lock`. `remove_stale()` deletes directories whose lock is free and that are older than a 120 s grace period, which covers the window between `mkdir` and taking the lock. It skips symlinks and plain files, and never follows them. Task 6.3 uses the same check for labelled containers and networks.
- **Sandbox trust is installed at runtime, not baked, and only when the bump-set is non-empty.** `ca.pem` is mounted read-only at `/project-sandbox-egress/ca.pem`. The entrypoint's root phase, which already runs the firewall, copies it into `/usr/local/share/ca-certificates/` and runs `update-ca-certificates` before dropping to the agent user. It then exports:
  - `NODE_USE_SYSTEM_CA=1` (already set in the image), which is what the Copilot CLI needs
  - `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`, pointing at the system bundle

  Baking a per-session CA would invalidate the image cache on every launch.
- **Alternative considered:** a per-workspace persistent CA, as upstream does. It was rejected because it needs a rotation procedure and widens the blast radius of a leaked key across sessions.

### 4. GitHub repository access is blocked

Sidecar mode blocks GitHub repository access. Smart per-repository access (separate read and write, LFS, release assets, GraphQL handling) is deferred to ROADMAP.md, "Smart GitHub repository access", which keeps the URL-shape table drafted here.

- **Repository hosts cannot be admitted:** `github.com`, `api.github.com`, `uploads.github.com`, `codeload.github.com`, `gist.github.com`, `raw.githubusercontent.com`, `gist.githubusercontent.com`, `media.githubusercontent.com`, `objects.githubusercontent.com`, `github-cloud.githubusercontent.com` and `release-assets.githubusercontent.com` (`egress_policy.GITHUB_REPOSITORY_HOSTS`).
  - The policy file's `allow` and `--extra-domain` reject them, including through a covering wildcard such as `*.github.com` or `*.githubusercontent.com`. Otherwise a single policy line could silently undo the block.
  - Copilot's own hosts under `githubusercontent.com` and `github.com` are listed individually in the Copilot profile, so the wildcard rejection does not affect them.
- **The one exception is `api.github.com` in Copilot `paths` mode.** The host is intercepted, and only `egress_policy.COPILOT_API_PATHS` pass. `/repos/…`, `/gists…`, `/graphql`, `/user/repos` and the rest get a labelled 403.
- **Flags and policy:**
  - `--allow-github` is rejected with `--egress sidecar` (the error points to firewall mode and the roadmap).
  - An `[egress.github]` table in the policy file is rejected with the same pointer.
  - `--agent copilot` enables only the Copilot profile (Decision 5), never repository access.
- `git`, `gh` and archive downloads against GitHub fail closed: resets for CONNECTs, or labelled 403s inside the intercepted `api.github.com`. The docs state this as expected behaviour.

### 5. Copilot profile

The Copilot profile is enabled by `[egress.copilot] enabled = true` in the policy, or implied by `--agent copilot`. It admits:

- **Spliced hosts** (`egress_policy.COPILOT_HOSTS`): `api.{individual,business,enterprise}.githubcopilot.com`. Copilot's own model traffic is never intercepted.
- **`api.github.com`, intercepted** (`COPILOT_API_ACCESS = "paths"`), with only `^/copilot_internal/` allowed (`COPILOT_API_PATHS`). Every other path gets a labelled 403, so the repository API stays blocked.

**Task 1.3 results** (`scripts/e2e-copilot-egress.py`, Apple `container`, individual Copilot plan, 2026-11-04):

| Phase | Result |
|---|---|
| capture (allow-all, everything intercepted) | Hosts used: `api.individual.githubcopilot.com` (`GET /models`, `POST /v1/messages`), `telemetry.individual.githubcopilot.com` (`POST /telemetry` ×3), `api.github.com` (`GET /copilot_internal/user` 200, `GET /copilot_internal/managed_settings` 404, `GET /repos/github/copilot-cli/releases` 200). |
| verify `deny` (no `api.github.com`) | **Fails**: Copilot exits 1 after 43 CONNECT attempts to `api.github.com`. The entitlement check is mandatory. |
| verify `paths` (`/copilot_internal/` and `/user`) | Copilot exits 0 with the expected reply. Denied: `telemetry.individual.githubcopilot.com` (missing from the then-profile) and the update check `/repos/github/copilot-cli/releases`. |
| block | Not run in the first pass, because no variant had passed under the strict grading. |
| **Re-run with the updated profile** | **All phases pass.** verify `paths`: Copilot succeeds, and the only denial is the expected update check. block: every probe is denied by the fixture. CONNECTs to `github.com` (including `git ls-remote`, exit 128), `raw.githubusercontent.com`, `codeload.github.com` and `gist.github.com` are reset. `api.github.com` `/repos/octocat/Hello-World`, `/repos/…/contents/README` and `/gists/public` get a labelled 403 (`server=squid/6.12`). |

Consequences, applied to `egress_policy.py`:
- **`paths` is the mode.** Interception of `api.github.com` (and so a sandbox-trusted, name-constrained CA) is required whenever the Copilot profile is on.
- **Allowed paths:** `/user` was never requested and is dropped, so `COPILOT_API_PATHS` is `^/copilot_internal/` only.
- **The update check stays denied.** It is repository API, and the CLI works without it. The e2e treats it as an expected denial: reported, but not a failure.
- **Host list:**
  - **Telemetry is blocked on purpose.** No telemetry should leave the sandbox. The CLI's `telemetry.<plan>.githubcopilot.com` hosts are not admitted, and the e2e treats their CONNECT denials as expected. The first run showed the CLI completes normally with telemetry denied (verify `paths`, exit 0).
  - The IDE-extension hosts from GitHub's published allowlist (`copilot-proxy`, `origin-tracker` and `copilot-telemetry.githubusercontent.com`, `collector.github.com`, `default.exp-tas.com`) and `api.githubcopilot.com` were unused by the CLI, and are dropped.
  - The business and enterprise hosts are plan counterparts of the captured individual hosts. They are not yet captured.
- **Firewall mode** also does not admit `telemetry.*.githubcopilot.com`, so Copilot CLI telemetry is blocked there too, which is intended. Its list still carries the unused IDE-extension hosts, including `copilot-telemetry.githubusercontent.com`. Trimming them is recorded in TODO.md, because it changes firewall mode.
- **The re-run confirmed the updated profile.** GHE.com tenant hosts follow the `add-copilot-agent-support` list and are not captured here. The business and enterprise hosts remain uncaptured.

### 6. Host-side policy file

`--egress-policy PATH` takes a TOML file. Without the flag, `$XDG_CONFIG_HOME/project-sandbox/egress.toml` is used if it exists; otherwise an empty policy plus the CLI flags applies.

```toml
[egress]
allow = ["pypi.org", "files.pythonhosted.org", "*.npmjs.org"]

[egress.copilot]
enabled = true
```

- **Location check.** After `resolve()`, the policy path is rejected if it lies inside the project directory, the worktree, the main repository's `.git`, or any other path mounted into the sandbox.
- `--extra-domain` is merged on top of the file. Agent provider hosts are added exactly as in firewall mode. GitHub repository hosts are rejected (Decision 4).
- Unknown keys are an error. `[egress.github]` gets a specific error pointing to the roadmap. `--dry-run` prints the effective policy.
- The rendered `squid.conf` lives in the session state directory, never in the project's `.project-sandbox/`.

### 7. Upstream parent proxy

- **Parent configuration.** In sidecar mode, `--internet-proxy URL` is parsed by the existing `proxy_endpoint` code and rendered as `cache_peer <host> parent <port> 0 no-query no-digest default [tls tls-cafile=…]` with `never_direct allow all`.
- **Policy is hostname-only in parent mode.** The `dst`-based SSRF ACLs are omitted, because they force local DNS resolution and corporate networks often resolve external names only at the proxy. IP-literal and non-443 CONNECT denials stay.
- **Intranet risk.** The parent proxy may reach intranet hosts, so the hostname allowlist is the only barrier to them. This is documented prominently.
- **Trust.**
  - `--ca-cert` inputs go into the sandbox image, as today, so spliced traffic intercepted by the corporate proxy validates.
  - The same inputs are mounted into the sidecar and appended to `tls_outgoing_options cafile=`, so bumped GitHub traffic validates certificates minted by the corporate proxy.
- **Docker/Podman.** The sidecar connects to the parent directly. On Docker Desktop, VPN routes are usually reachable from containers.
- **Apple `container`.** The sidecar connects to the existing host relay at `host.docker.internal:<port>` over HTTP, and the relay re-originates TLS on the host using the host's VPN routes and DNS. In sidecar mode the relay port is chosen dynamically per session, because nothing baked into an image depends on it. This fixes the ROADMAP concurrency item for this mode. Firewall-mode relay behaviour is unchanged.
- **Squid version check.** Bumped and spliced traffic must actually be tunnelled via CONNECT to the `cache_peer`, not sent DIRECT. Spike 0.2 confirmed this on the candidate image (results below).
- **Rendered parent mode (task 5.4):**
  - `cache_peer <host> parent <port> 0 no-query no-digest default name=upstream [tls [tls-cafile=/run/egress-sidecar/upstream-ca.pem]]` with `never_direct allow all`.
  - `private_dst` is omitted, and IP-literal and port denials stay.
  - With `--ca-cert`, the CLI concatenates the CAs into `upstream-ca.pem`. The entrypoint stages it next to the session CA, and `tls_outgoing_options cafile=` adds it to Squid's outgoing trust.
  - `squid_config.upstream_for()` maps the endpoint: a remote proxy is dialled directly; a host-loopback proxy as `host.docker.internal:<port>`; on Apple `container`, the host relay as plain HTTP `host.docker.internal:<relay port>`.
  - Live check against a logging parent: spliced `example.com`, intercepted `api.github.com` and plain-HTTP requests all reached the parent as `CONNECT`/`GET`; an unlisted host was reset locally and never reached it.
- **Spike 0.2 results (Apple `container` 1.4.1, 2026-11-04).**

  The candidate image is `alpine:3.22.1` + `squid-6.12-r0` (Squid 6.12, with `/usr/lib/squid/security_file_certgen`), `openssl-3.5.9-r0` and `su-exec-0.2-r3`. Three containers ran on the default network:
  - **Parent:** a plain Squid with `http_access allow all`, logging `%rm %ru`.
  - **Candidate:** `http_port 3128 ssl-bump tls-cert=… generate-host-certificates=on`, `sslcrtd_program security_file_certgen`, `cache_peer <parent> parent 3128 0 no-query no-digest default` and `never_direct allow all`. Policy: allowlist `example.com` (splice) and `api.github.com` (bump, with only `/zen` allowed). It used the Decision 2 pipeline: `TCP_RESET` CONNECT denials, the `sni_match` helper, and `on_unsupported_protocol respond all`.
  - **Client:** `curl`, `openssl s_client -proxy` and raw `nc`. The CA was EC P-256, `pathlen:0`, with critical name constraints `github.com`, `.github.com` and `.githubusercontent.com`.

  | Probe | Candidate log | Parent log | Client sees |
  |---|---|---|---|
  | Splice `https://example.com/` | `TCP_TUNNEL/200 … splice` | `TCP_TUNNEL/200 CONNECT example.com:443` | 200 |
  | Bump `https://api.github.com/zen` (CA trusted) | `TCP_MISS/200 GET https://api.github.com/zen … bump` | `TCP_TUNNEL/200 CONNECT api.github.com:443` | 200 |
  | Bump `https://api.github.com/user` (path not allowed) | `TCP_DENIED/403 GET … bump ERR_ACCESS_DENIED` | none | 403 page |
  | Bump without CA trust | `ERR_SECURE_ACCEPT_FAIL` | CONNECT (server-first peek) | TLS verify error |
  | Plain `http://example.com/` | `TCP_MISS/200 GET` | `TCP_MISS/200 GET http://example.com/` | 200 |
  | Plain `http://api.github.com/zen` (bump-set host) | `TCP_DENIED/403` | none | 403 page |
  | CONNECT `www.wikipedia.org:443` (unlisted) | `TCP_DENIED/200 … TCP_RESET cause=host-not-allowed` | none | 0 bytes, reset |
  | CONNECT `example.com:8443` | `… TCP_RESET cause=port-not-allowed` | none | 0 bytes, reset |
  | CONNECT `1.1.1.1:443`, `169.254.169.254:443` | `… TCP_RESET cause=ip-literal` | none | 0 bytes, reset |
  | Same denials with plain `http_access deny` (no `TCP_RESET`) | `TCP_DENIED/200 … bump ERR_ACCESS_DENIED` | none | **`200`**, then a TLS error on the minted certificate |
  | SNI `api.github.com` in CONNECT `example.com` (both allowed) | `… terminate` | none | EOF in handshake |
  | SNI `www.wikipedia.org` in CONNECT `example.com` | `… terminate` | none | EOF |
  | SNI `example.com` in CONNECT `api.github.com` | `… terminate` | none | EOF |
  | No SNI, splice host or bump host | `… terminate` (helper). With `ssl::server_name --client-requested` instead: **spliced/bumped** | none | EOF |
  | Raw plaintext or `SSH-2.0` after CONNECT `example.com`/`api.github.com` | `peek ERR_PROTOCOL_UNKNOWN` | none | `200`, then close |

  Other findings:
  - **Bumped requests are tunnelled to the parent.** Squid 6.12 sends a `CONNECT host:443` to the `cache_peer` for both spliced and bumped connections and originates TLS inside that tunnel. It never went DIRECT under `never_direct allow all`. A TLS `cache_peer` (`tls tls-cafile=`) and the Apple relay parent were not exercised; they are covered by task 5.4 and the e2e suite.
  - **Logging as non-root on Apple `container`:** a Squid that starts as root and drops to `squid` fails with `FATAL: Cannot open '/dev/stdout' for writing`, because the console fd is root-owned. Running Squid as `squid` from the start (`su-exec squid sh -c 'squid -N -f … 2>&1 | cat'`) makes stdout a pipe that `squid` owns and works. Decision 9 follows from this.
  - The harmless startup warning `no_suid: setuid(0): Operation not permitted` comes from helpers when Squid is not root.
  - The spike configs live outside the repo. The final config is produced by task 5.2.
- **Denial labelling.** Our denials carry our `deny_info` label. Parent denials pass through unchanged. The session-end summary attributes blocks to `local policy` or `upstream proxy`.

### 8. Lifecycle and observability

- **Start order** (`egress_sidecar.session()`, a context manager):
  1. Render the policy and CA into the locked session directory.
  2. Create the network: `sbx-egress-<sid>`, `--internal`, labelled; Podman adds `--disable-dns`.
  3. Read its IPv4 subnet (for the forwarders' `range=`).
  4. Start the sidecar (`project-sandbox-egress-<sid>`, `-d`, `--tmpfs /run`, the session directory mounted read-only at `/project-sandbox-egress`). Apple uses `--network default --network <int>`; Docker and Podman run it on the default network and then `network connect`.
  5. Read the sidecar's internal IPv4 from `inspect`.
  6. Wait until it is ready, with a bounded timeout (30 s). From inside the sidecar, a plain-HTTP `GET http://sidecar-readiness.invalid/` must come back with the `project-sandbox-egress: host-not-allowed` page. This works on every runtime without reaching the internal IP from the host. BusyBox `nc` drops the connection when stdin ends, so the probe keeps stdin open for one second. On timeout, the error includes the last 20 sidecar log lines.
  7. Yield the IP and subnet to the caller, which starts the sandbox.
- **Teardown** runs in the context manager's `finally`: `rm -f` the sidecar (also after a failed `run -d`), remove the network, then the session directory (including the CA key) is deleted. Unit tests cover success, readiness timeout, sandbox-start failure, network-create failure and sidecar-start failure, each asserting the cleanup calls.
- **Live check** (Apple `container` 1.4.1 and Docker Desktop 29.9.2): ready in 2.0 s and 1.3 s respectively. From a container on the internal network, `CONNECT example.com:443` got `200`, an unlisted host got zero bytes, and direct egress was blocked. After the block, the network, sidecar and CA directory were all gone.
- **Orphan cleanup** (`egress_sidecar.cleanup_orphans()`): resources are labelled `project-sandbox.egress.session=<sid>`. The next launch, and `project-sandbox doctor`, list labelled containers and networks: Docker and Podman with `--filter label=…`; Apple's lists, filtered client-side. They remove those whose session directory holds no live lock (`session_ca.is_live`), containers before networks, and then the stale session directories. Listing failures are ignored, so cleanup never blocks a launch.
- **Apple relay port (sidecar mode):** `internet_proxy.relay(config, sidecar=True)` binds any free loopback port per session, so concurrent sessions using the same proxy no longer collide. Firewall mode keeps the derived fixed port, which the image firewall bakes in.
- **Logs (task 8.1).** At teardown, `session(log_path=…)` saves the sidecar's log to `<state>/sessions/<session id>.egress.log`, next to the session record, before removing the container. This is best effort and never blocks teardown. The CLI then prints a summary built by `egress_log`:
  - **Bounded:** at most 10 targets, then "… and N more".
  - **Cause per target:** the `sbx_cause` label. `bump=terminate` rows count as `sni-mismatch`, and failed peeks as `non-tls-tunnel`. Path denials show the host and the first two path segments.
  - **Attribution:** local policy, or the upstream proxy when a parent refused a CONNECT or answered 407.
  - **Remedy:** "add it to egress.allow in <policy>", or the default path to create. Readiness probes are ignored.
  - Example from a real Apple launch:

  ```
  Egress denials this session:
    example.com  host-not-allowed  x1
    github.com   host-not-allowed  x1
    Full log: ~/.local/state/project-sandbox/sessions/<id>.egress.log
    To allow a host, create ~/.config/project-sandbox/egress.toml with [egress] allow = [...]
  ```
- **Dry-run** shows the planned network, sidecar argv, effective policy and CA constraints. It writes no files, generates no keys and creates no networks.

- **CLI wiring.** A real `--egress sidecar` launch does the following:
  - It merges the policy before any worktree exists. It is location-checked against the project, the planned workspace, `.git`/`.jj` and every `--mount` source.
  - It renders the sidecar firewall and entrypoint variants.
  - Local services become forward ports on the sidecar, and no host forwarding plans are started.
  - On Apple `container`, it binds the remote-proxy relay on a per-session port.
  - Right before the sandbox runs, it cleans orphans, builds or reuses the sidecar image, and starts `egress_sidecar.session()` on an `ExitStack` that the launch's `finally` closes first.
  - The sandbox joins the internal network, with the `sandbox/` mount and `HTTP(S)_PROXY=http://egress-proxy:3128`.
- **Not supported in this change** (rejected up front, listed in TODO.md): a remote `--agent-proxy`, because the sandbox would need a direct route to it, and `--copilot-host` GHE.com tenants, because the profile covers github.com accounts only.
- **Real launch check** (2026-11-05, `--agent bash --egress sidecar --extra-domain pypi.org`, prompt-driven probes inside the real sandbox image): on Apple `container` and Docker, `pypi.org` returned 200 through the sidecar, while `example.com`, `github.com`, direct egress and DNS were blocked. Afterwards no `sbx-egress-*` network, sidecar container or egress session directory remained. The Apple session took about 29 s, including a 19 s image build.

### 9. Sidecar image

- **Build context:** `sidecar_image.py` renders `templates/egress-sidecar/` (`Dockerfile`, `entrypoint.sh`, `sni-match`) into `$XDG_STATE_HOME/project-sandbox/egress-sidecar-image/<hash>/`, never into a project.
- **Pins:**
  - Base `alpine:3.22.1@sha256:4bcff639…11d1`.
  - Package `squid~6.12`. apk's `~` pins the Squid release but accepts Alpine's security revisions of it. An exact `-rN` pin would break builds once the branch repository drops that revision.
  - The image also ships `socat` (local-service forwarding, Decision 1), `su-exec`, and the `sni-match` external ACL helper (Decision 2).
- **Caching:** the image tag is `project-sandbox-egress-sidecar:<first 16 hex of sha256 over the rendered files>`. An existing image with that tag is reused, and any pin or template change produces a new tag. No separate build-state file is needed.
- **Pin updates:** `scripts/update-pins.py` (`collect_squid_sidecar_update`) updates the Alpine tag and digest (Docker Hub) and the Squid release (from that branch's `APKINDEX`) together. It keeps `sidecar_image.py` and the `copilot-egress` e2e fixture in sync.
- **Entrypoint, running as root:**
  - It stages the read-only `/project-sandbox-egress/{ca.pem,ca-key.pem,squid.conf}` into `/run/egress-sidecar` (the sidecar runs with `--tmpfs /run`), initialises the `security_file_certgen` database, and changes ownership to `squid` (key 0600).
  - It starts one `socat` per `SBX_FORWARD_PORTS` entry as `nobody`, with `range=$SBX_INTERNAL_SUBNET`. Ports must be in 1024–65535, and a subnet is required.
  - It `exec`s Squid as `squid` via `su-exec`, so **Squid is PID 1** and receives the runtime's stop signal. Its stdout/stderr go to a FIFO owned by `squid`, relayed by `cat` to the container log, because Squid cannot reopen a root-owned console fd (spike 0.2).
  - The rendered `squid.conf` should set `shutdown_lifetime 1 seconds` (task 5.2).
- **Apple `container` 1.4.1 builder bug:** a `COPY` whose source is a directory silently copies nothing. The error pages are therefore copied file by file, in a JSON-form `COPY`, and a renderer test asserts that every `COPY` source is a rendered file.
- **Verified on Apple `container` 1.4.1:** the image builds; `ps` shows Squid as PID 1 (`squid`), `socat` as `nobody` with `range=`, and the certgen helpers as `squid`; access logs reach `container logs`. A matching SNI is spliced and a mismatched one terminated by `sni-match`. `container stop` takes about 2 s, and Squid exits normally.

### 10. Verification probe set (task 1.1)

The probes are ported from [internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally), commit `997bad78af6021e092aea0f62e76279f01db305d` (2026-10-25). The sources are `src/internet_proxy_locally/checks/egress/`, documented in `docs/egress-checks.md`. The probes are re-implemented in `scripts/e2e-egress-sidecar.py`, not imported, because upstream grading counts resets as inconclusive.

**Grading differs from upstream.** Upstream requires a 4xx on CONNECT denials and treats resets as `error`. Our CONNECT-level denials are deliberately `TCP_RESET` (Decision 2). Our grading is:
- **Denial:** zero bytes followed by a close or reset, and a matching `sbx_cause` row in the sidecar access log. A `200` before the close counts as **fail**.
- **Plain-HTTP and bumped denials:** a 403 whose body carries the `project-sandbox-egress:` label for the expected cause. Any other ≥400 is `error`, not pass, which avoids upstream's "any 4xx passes" caveat.

| e2e requirement item | Upstream check(s) | Ported as | Notes |
|---|---|---|---|
| allowed splice | `allowed-https`, `allowed-http` | yes | Adds an HTTP GET after TLS and requires the sidecar log to show `splice`. |
| unlisted host | `blocked-host-connect`, `blocked-host-http` | yes | Regraded as above. |
| IP literal (part of the hostname policy) | `direct-ip-connect`, `loopback-ipv4`, `loopback-ipv6`, `rfc1918-ipv4`, `link-local-ipv4`, `private-ipv6` | yes, as one table-driven check | Expected cause is `ip-literal` (the literal rule fires first, as upstream's caveat notes). Port 443 is used so the port rule does not mask it, plus one port-80 sample expecting `port-not-allowed`. |
| metadata | `metadata-endpoint`, `dns-private-ipv4`, `dns-private-ipv6` | yes | The literal is `ip-literal`. The `nip.io`/`sslip.io` names exercise the resolved-address rule in **direct mode only**: they are added to the test allowlist and expect the private/metadata cause. In parent mode they are skipped, because policy is hostname-only (Decision 7). |
| SNI mismatch | `connect-sni-mismatch` | yes, extended | Upstream's case (CONNECT `pypi.org`, SNI `files.pythonhosted.org`) plus no-SNI, a bump-set host with a foreign SNI, and an unlisted SNI. Each expects `terminate`, with the matching-SNI control succeeding. |
| raw tunnel | `connect-raw-tunnel` | yes, extended | A plaintext GET and an `SSH-2.0` banner, against a splice host and a bump-set host. Zero bytes after the `200` = pass. Spike 0.2 observed this. |
| (lifecycle sanity) | `concurrency-sanity` | yes | Ten concurrent CONNECTs to an allowed host through one sidecar. |
| direct-egress denial | none (upstream does not run a sandbox) | new | Run from the sandbox with proxy variables unset: public IPv4/IPv6, DNS, the internal gateway, and host addresses. |
| GitHub repository access blocked | none | new | The `copilot-egress` block probes (git, REST repos/contents, raw, codeload, gists), run against the sidecar instead of the fixture. |
| Copilot profile works | none | new | A Copilot session through the sidecar with the profile from Decision 5 (task 1.3 result). |
| parent chaining | none | new | `fixture_network.py` intercepting parent with its own CA. The parent log must show the CONNECTs. |
| local-service forwarding | none | new | Decision 1 "Local services", including the `range=` refusal from a default-network container. |

**Results (2026-11-05, `scripts/e2e-egress-sidecar.py`, suite `egress-sidecar`):** all checks passed on Apple `container` 1.4.1 (32 for `policy` and `parent`, plus 2 for `forward`), on Docker Desktop 29.9.2 (34/34) and on rootless Podman 6.0.1 in a macOS machine (34/34). As implemented:
- Each check is graded on the client view inside the sandbox and on the sidecar log's cause.
- The metadata-by-name probe follows the documented known limitation: a TLS failure after `200`, with `private-address` logged and no upstream connection.
- Parent chaining used the `copilot-egress` Squid fixture in intercept mode, with its CA passed as `--ca-cert`.
- The `forward` check reached a loopback HTTP listener on the host through the sidecar, and verified that a default-network peer is refused.
- The Copilot-profile row is covered by the `copilot-egress` suite rather than repeated here, because it is billable.

**Not ported:** `dns-rebinding`, `dns-mixed-answers` and `ptr-allowlist`. They need upstream's lab DNS fixture (`rebind.fixture.test`), and they test Squid's resolved-address checks, which spike 0.2 did not change. Upstream's `docs/findings.md` records Squid passing them. They are left as TODO.md candidates and are not required by the e2e requirement.

## Risks / Trade-offs

- **Apple multi-network support** was verified on 1.4.1 by spike 0.1. → `doctor` should still check the runtime version, because older releases are untested.
- **The host-only network reaches host listeners** (gateway and the loopback redirect). → The sandbox firewall stays mandatory in sidecar mode, and `--no-firewall` is rejected.
- **Squid `cache_peer` handling of bumped and spliced traffic** depends on the version. → Spike 0.2 verified 6.12. A pin bump must re-run the e2e parent-chaining check.
- **CONNECT denials are resets, not pages.** Squid forces a bump to serve errors on `ssl-bump` ports, so CONNECT-level denials use `TCP_RESET`. → Clients see "connection reset". The labelled cause is in the access log and the session-end summary, and the docs explain this.
- **The SNI check depends on an external helper process.** → It is small, stateless, bundled in the image and covered by e2e. A crashed helper fails closed, because the ACL does not match and the connection is terminated.
- **Interception of `api.github.com` for Copilot** (required, per task 1.3). Only the Copilot CLI talks to that host in sidecar mode, and it trusts the system store through `NODE_USE_SYSTEM_CA=1`. → Without the Copilot profile, nothing is intercepted.
- **Hostname-only policy in parent mode** loses IP-level SSRF protection and lets intranet names through the parent if allowlisted. → Strict hostname validation, no IP literals, and documentation.
- **The CA key on the host** is readable by the user's other processes during the session. → Mode 0600, per-session lifetime, name constraints, deletion at teardown.
- **Startup cost.** A second container and a network per session. → Image cached, readiness timeout bounded. Measured in the e2e suite.
- **Blocking GitHub repository access breaks `git`/`gh` against GitHub inside sidecar sessions.** → This is intended for now: firewall mode with `--allow-github` remains available, and per-repository access is on the roadmap.
- **Policy file outside the repo is less discoverable.** → `--dry-run` and the session-start banner name the effective policy path.

## Migration Plan

This mode is opt-in, and there is no migration for existing users. Firewall mode and `--internet-proxy` without `--egress sidecar` behave as before. Rollback means omitting `--egress sidecar`.

## Open Questions

- ~~Does Apple `container` allow two `--network` flags on one run? (Spike 0.1)~~ Yes, on 1.4.1. See Decision 1.
- ~~Does Squid 6.x on the pinned image tunnel bumped and spliced traffic through `cache_peer` under `never_direct`? (Spike 0.2)~~ Yes, on 6.12. See Decision 7.
- ~~Which hosts does the Copilot CLI use, and does it need `api.github.com`?~~ It needs `/copilot_internal/` on `api.github.com` (`paths` mode). See Decision 5 for the host set. A re-run with the updated profile passed every phase, including the block probes.
- ~~How does the sandbox reach local services on Docker and Podman in sidecar mode?~~ The sidecar forwards them with `socat` on all runtimes. See Decision 1, "Local services".
- Does the Podman pasta local-service strategy (rootless Podman, loopback-only services) work from a sidecar that is also attached to an internal network? This is checked in the e2e suite (task 9.1). If it does not, sidecar mode on that configuration rejects local services with a clear error.
