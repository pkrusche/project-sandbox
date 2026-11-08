# Design

## Context

Today there are two egress modes (see proposal.md, Why):

```
 firewall (default)                      firewall + --internet-proxy
 sandbox: iptables + ipset               sandbox: iptables admits only proxy IP:port
 dig domain → pin IPs once               HTTP(S)_PROXY → user-managed proxy
```

Firewall mode with `--internet-proxy http://127.0.0.1:<port>` already works with a filtering proxy on host loopback, such as `internet-proxy-locally`. `_local_services()` treats the proxy as a local service. `local_service_network` forwards it into the sandbox with the runtime's strategy (Apple's `203.0.113.113` localhost alias, Docker Desktop's and Podman machine's host alias). The `internet-proxy` firewall policy admits only that endpoint and the other local services. This change runs a managed Squid in that role.

This design starts from the pre-sidecar base (`ptyttxkk`). It reuses the decisions and spike results of the unreleased `squid-sidecar` line, whose sandboxes sat on an internal network behind the sidecar. The full spike logs are in jj history:

- `add-squid-egress-sidecar` design: `jj file show -r mpruptuo openspec/changes/archive/2026-11-06-add-squid-egress-sidecar/design.md`
- `global-egress-sidecar-macos` design: `jj file show -r sykznxwo openspec/changes/archive/2026-11-07-global-egress-sidecar-macos/design.md`
- Linux forwarding spikes: `jj file show -r vrxzuotu openspec/changes/global-egress-sidecar-linux/design.md`

The squid-sidecar implementation (`rymymutp` and its ancestors) is the reference for every module named here. Most of it ports unchanged; Decision 1 and 2 list what is dropped.

## Goals / Non-Goals

**Goals:**
- Filter by hostname, re-resolving on every request, with wildcards; no IP pinning.
- Keep the sandbox side identical to firewall mode with an Internet proxy, so the forwarding code that already works per runtime is reused.
- Starting and reusing the sidecar is idempotent and serialized: two launches never create two sidecars.
- Allowlist changes never need a restart.
- The agent cannot alter the policy that governs it, now or in later sessions.
- Use TLS interception only where path rules need it: at most Copilot's token exchange on `api.github.com`.

**Non-Goals:**
- A sandbox without a route to the network. The squid-sidecar line had this through an internal network; this design gives it up for the forwarding simplicity (Decision 1, Risks).
- Per-sandbox policy or denial attribution.
- Transparent interception: clients that ignore proxy variables fail closed.
- Request-body inspection, DLP, ICAP, credential injection, upstream authentication.
- Changing firewall-mode behaviour.
- Docker Engine and rootless Podman (`egress-sidecar-linux`).

## Decisions

### 1. Topology: a published sidecar, reached like a local service

```
 sandbox (default network)                       host                         sidecar (default network)
 HTTP(S)_PROXY=http://host.docker.internal:3129
 firewall: DROP, admit sidecar endpoint      ──▶ 127.0.0.1:3129 ──publish──▶ squid :3128 ──▶ Internet / parent
           + this session's local services   ──▶ 127.0.0.1:8787, :11434 (agent proxy, Ollama)
```

- **The sidecar** is `project-sandbox-egress`, one per runtime, on the runtime's default network. It publishes Squid's port 3128 on `127.0.0.1:<port>`. The port is `[egress.sidecar] port`, default 3129 (3128 is commonly taken by a host Squid or `internet-proxy-locally`). Only loopback is published.
- **The sandbox** is built and run like firewall mode with `--internet-proxy http://127.0.0.1:<port>`:
  - The sidecar endpoint is an Internet-proxy destination. The `internet-proxy` firewall policy admits only it and this session's local services, keeps default DROP and blocks DNS and IPv6.
  - The sidecar endpoint and the local services are forwarded with the existing `local_service_network` plans: `apple-configured-host-alias`, `docker-desktop-host-alias`, `podman-native-host-alias`.
  - The proxy variables come from the same code as firewall mode (`HTTP(S)_PROXY=http://host.docker.internal:<port>`, `NO_PROXY` with the local-service bypass entries).
  - A remote `--agent-proxy` is admitted as in firewall mode with `--internet-proxy`: a direct, port-pinned firewall destination that does not pass through Squid.
- **The port is baked into the sandbox image**, like any firewall-mode destination. A launch reads it from `egress.toml` before rendering. If the running sidecar publishes a different port (restart drift), the launch fails and names `project-sandbox-egress restart`, because the sandbox could not reach it.
- **No per-session listener on these runtimes.** Each strategy reaches host loopback through a runtime alias, so concurrent sandboxes share the published port without binding anything per session. Spike 0.1 verified this on all three runtimes (below).
- **What is dropped from the squid-sidecar line:** the internal network `sbx-egress`, the in-sidecar `socat` forwarders and `sidecar/forwarders`, `[egress.sidecar] forward_ports`, `project-sandbox-egress forward`, the per-session `egress.conf` (`sidecar_ip`, `forward_ports`), the `sidecar` firewall policy and `init-firewall-sidecar.sh.j2`, the `/etc/hosts` rewrite, `SBX_INTERNAL_SUBNET` and `socat` in the image.
- *Alternative:* the internal network with sidecar forwarders (the squid-sidecar line). Rejected: it needs a forwarder per local service inside the sidecar, and on Linux a host-side helper per runtime, one of them inside Podman's rootless network namespace (see the Linux spikes). Its one advantage, no route even if the sandbox firewall failed, is given up (Risks).
- *Alternative:* reach the sidecar's container IP directly on the default network. Rejected: the IP changes on restart and would have to be a runtime input, and container-to-container reachability differs per runtime.
- *Fallback, had spike 0.1 shown that Apple `container` cannot publish on loopback:* the Apple sandbox would have admitted the sidecar's default-network address, read from `inspect` and passed through the per-session mount (Decision 8); publishing on all interfaces is not acceptable. Not needed: see the spike results.

**Spike 0.1 results (2026-11-07, macOS).** On each runtime, a `python:3.12-slim` HTTP server ran with `--publish 127.0.0.1:3129:3128`. Two `alpine:3.22.1` containers then fetched `http://host.docker.internal:3129/ok` five times each, concurrently, with the firewall-mode host-alias strategy: the DNS alias on Apple, `--add-host host.docker.internal:host-gateway` on Docker and Podman. The script is not kept in the repository; the `egress-sidecar` e2e suite's `policy:endpoint-loopback-only` check repeats the loopback part.

| Runtime | Host listener | Host 127.0.0.1 | LAN, VM-bridge and Tailscale addresses | `host.docker.internal` in the clients | Clients |
|---|---|---|---|---|---|
| Apple `container` 1.4.1 | `container` on `127.0.0.1:3129` | answered | refused | `203.0.113.113` (localhost alias) | 5/5 and 5/5 |
| Docker Desktop 29.9.2 | `com.docker.backend` on `127.0.0.1:3129` | answered | refused | `fdc4:f303:9324::254` (IPv6 host gateway, listed first) | 5/5 and 5/5 |
| Podman machine 6.0 (applehv) | `gvproxy` on `127.0.0.1:3129` | answered | refused | `192.168.127.254` | 5/5 and 5/5 |

Every runtime publishes on loopback only, and concurrent containers reach the published port through the existing local-service strategy, so Decision 1 holds as written. On Docker Desktop the host gateway resolves to IPv6 first. The `internet-proxy` firewall already pins every resolved address of each family it can enforce, so this needs no change, but the IPv6 path is what the sandbox will normally use there.

### 2. Squid policy pipeline (unchanged from the squid-sidecar line)

```
 CONNECT host:port ─▶ port == 443?                       no ─▶ TCP_RESET (no 200)
                      host is an IP literal?             yes ─▶ TCP_RESET
                      host ∈ allow (dstdomain)?           no ─▶ TCP_RESET
                      [direct mode] dst ∈ metadata/private? yes ─▶ TCP_RESET
                      ─▶ 200 Connection established
 ssl_bump step1  peek
 ssl_bump step2  SNI present AND SNI == CONNECT host? (sni-match)  no ─▶ terminate
                 host ∈ bump-set?                                 yes ─▶ bump ─▶ path rules (labelled 403)
                 otherwise                                            ─▶ splice
 non-TLS bytes after 200 ─▶ peek fails ─▶ on_unsupported_protocol respond ─▶ closed, no upstream connection
```

Facts the config depends on (Squid 6.12, spike 0.2 of `add-squid-egress-sidecar`):
- **CONNECT-level denials use `deny_info TCP_RESET`.** On an `ssl-bump` port, Squid bumps every denied CONNECT to serve its error page over TLS (`sslBumpAccessCheck()`, "Force bump action on error"), so a plain `http_access deny` answers `200` and mints a certificate. With `TCP_RESET` the client gets zero bytes and a reset. The log still says `TCP_DENIED/200` with `err_code TCP_RESET`, so the parser keys on `err_code`.
- **Causes are logged with `note sbx_cause <cause> <acls>`.** Each note uses the cause's real condition ACLs (an always-true ACL would label everything). Order: port, IP literal, private address, host, then the intercepted-host causes. `deny_info` binds to the last ACL on a deny line, so each cause also has an always-true marker ACL.
- **SNI equality needs the `sni-match` external ACL helper.** `ssl::server_name --client-requested` falls back to the CONNECT host when SNI is absent, so no built-in ACL detects a missing SNI, and per-entry pairs cannot express equality for wildcards. The helper (`%ssl::>sni %>rd`, ttl 60) returns OK only when SNI is present and equals the CONNECT host, case-insensitively. A crashed helper fails closed.
- **Raw tunnels:** `on_unsupported_protocol respond all`; logged as `peek ERR_PROTOCOL_UNKNOWN`.
- **Plain HTTP (80)** uses the same allowlist; bump-set hosts over plain HTTP get a labelled page.
- **An `ssl-bump` port needs `tls-cert` even for peek/splice only;** without it Squid 6.12 segfaults at startup. Hence a CA always exists (Decision 3).
- **Rendered details:** caching off, `host_verify_strict on`, no `dns_defnames`, `forwarded_for delete` (keeps client addresses out of upstream requests; `via off` was dropped because Squid warns that HTTP requires Via), labelled `ERR_SBX_*` pages under `error_directory /etc/squid/sbx-errors` reading `project-sandbox-egress: <cause>`, `::ffff:0:0/96` not in `private_dst` (Squid stores IPv4 as mapped addresses), `shutdown_lifetime 1 seconds`, `pid_filename /run/egress-sidecar/squid.pid` (required by `squid -k reconfigure`).
- **Log format** `sbx`: `%ts.%03tu %Ss/%>Hs %rm %ru sni= bump= err= cause= peer=%<a client=%>a`. `client` is the address of whatever connected to the published port (the runtime's port forwarder), not the sandbox; it is kept for debugging only.
- **Known limitation:** a resolved-address denial (an allowlisted name resolving to a private address) answers `200` first, because the `dst` check waits for DNS; the client then fails TLS on a certificate outside the CA's constraint, and no upstream connection is made. IP-literal metadata requests are reset before `200`.
- WebSocket upgrades stay disabled, matching upstream.
- Every hostname inserted into the config is validated against strict patterns first.

### 3. Selective interception and the sidecar CA

- **Bump-set:** empty, or `api.github.com` alone when the Copilot profile is on.
- **Name constraints:** the bump-set's registrable domain, `permitted;DNS:github.com`. It cannot be narrower: `generate-host-certificates` mimics the origin certificate, and GitHub serves `CN=*.github.com` with SANs `*.github.com, github.com`; a constraint of only `api.github.com` failed every intercepted request with `permitted subtree violation`. With an empty bump-set the constraint is `permitted;DNS:invalid` (RFC 6761) and the CA is never given to a sandbox.
- **Generation** on the host with `cryptography`: EC P-256, `BasicConstraints(ca=True, path_length=0)`, `KeyUsage(keyCertSign, cRLSign)`, critical `NameConstraints`, common name `project-sandbox egress <runtime>`. Lifetime `[egress.ca] lifetime_days`, default 7, range 1–30.
- **Reuse:** kept across `up`/`restart` while valid for at least 2 more days and its constraints match the Copilot setting; otherwise regenerated; `restart --new-ca` always regenerates. `down` deletes the key.
- **Key handling:** `ca-key.pem` 0600 in a 0700 directory, written with `O_EXCL | O_NOFOLLOW`, mounted read-only only into the sidecar. The sidecar entrypoint copies it to tmpfs for `security_file_certgen` and drops privileges.
- **Sandbox trust is installed at container start, never baked,** and only with interception: see Decision 8. Baking would change the image on every CA rotation.
- **Launch checks:** with interception active, warn when the CA has less than 2 days left and refuse when it has expired. Without interception, say nothing.

### 4. GitHub repository access is blocked

- **Hosts that cannot be admitted** (`GITHUB_REPOSITORY_HOSTS`): `github.com`, `api.github.com`, `uploads.github.com`, `codeload.github.com`, `gist.github.com`, `raw.githubusercontent.com`, `gist.githubusercontent.com`, `media.githubusercontent.com`, `objects.githubusercontent.com`, `github-cloud.githubusercontent.com`, `release-assets.githubusercontent.com`. The policy's `allow` and `allow add` reject them, including through a covering wildcard (`*.github.com`, `*.githubusercontent.com`).
- **The one exception** is `api.github.com` in Copilot `paths` mode: intercepted, with only `^/copilot_internal/` allowed; `/repos/…`, `/gists…`, `/graphql` and the rest get a labelled 403.
- `--allow-github` and an `[egress.github]` table are rejected in sidecar mode, pointing to firewall mode and ROADMAP.md ("Smart GitHub repository access").

### 5. Copilot profile

Enabled by `[egress.copilot] enabled = true`. It splices `api.{individual,business,enterprise}.githubcopilot.com` and intercepts `api.github.com` in `paths` mode. A launch with `--agent copilot` fails when the configured or running sidecar lacks the profile.

Captured with `scripts/e2e-copilot-egress.py` (Apple `container`, individual plan, 2026-11-04): the CLI uses `api.individual.githubcopilot.com` (`GET /models`, `POST /v1/messages`), `telemetry.individual.githubcopilot.com` and `api.github.com` (`/copilot_internal/user`, `/copilot_internal/managed_settings`, and the update check `/repos/github/copilot-cli/releases`).
- Without `api.github.com` the CLI exits 1 after 43 CONNECT attempts: the entitlement check is mandatory, so `paths` mode is required.
- Telemetry is blocked on purpose; the CLI completes normally without it.
- The update check stays denied (repository API); the e2e treats it as an expected denial.
- The IDE-extension hosts from GitHub's published list were unused by the CLI and are not admitted. The business and enterprise hosts are plan counterparts, not yet captured.
- The re-run with this profile passed every phase, including the block probes (`git ls-remote` exit 128, resets for `github.com`, `raw.githubusercontent.com`, `codeload.github.com`, `gist.github.com`; labelled 403s for `/repos/…` and `/gists/public`).

### 6. Policy file and editing

```toml
[egress]
allow = ["pypi.org", "files.pythonhosted.org", "*.npmjs.org"]
[egress.copilot]   enabled = false
[egress.upstream]  url = "https://proxy.corp.example"   ca_cert = ["~/corp.pem"]
[egress.ca]        lifetime_days = 7
[egress.sidecar]   port = 3129   memory = "256M"   cpus = 1
```

- Read only from `$XDG_CONFIG_HOME/project-sandbox/egress.toml`; missing means built-in defaults. There is no per-launch policy flag.
- Unknown keys are an error; `[egress.github]` gets a specific pointer. The upstream URL uses `proxy_endpoint` validation. `port` is 1024–65535.
- The effective allowlist is `allow` plus the provider hosts of every supported agent.
- **Location check:** after resolving symlinks, a launch is rejected when `egress.toml` or the state directory lies inside the project, the worktree, the main repository's `.git`/`.jj`, or any mount source.
- **Edits** (`allow add|remove`) use `tomlkit`, so comments and unrelated keys survive; each edit is re-validated through the parser and the file is replaced atomically. Reading stays with `tomllib`.
- Rendered config lives in the state directory, never in the project.

### 7. Upstream parent proxy

- Rendered as `cache_peer <host> parent <port> 0 no-query no-digest default name=upstream [tls [tls-cafile=/run/egress-sidecar/upstream-ca.pem]]` with `never_direct allow all`.
- **Policy is hostname-only in parent mode:** `private_dst` is omitted, because it forces local DNS and corporate networks often resolve external names only at the proxy. IP-literal and port denials stay. The parent may reach intranet hosts, so the allowlist is the only barrier to them; documented.
- **Trust:** `ca_cert` files are concatenated into `upstream-ca.pem`, trusted by Squid (`tls_outgoing_options cafile=`) and installed into sandboxes at container start (Decision 8). `--ca-cert` is therefore rejected in sidecar mode.
- **Reaching the parent:** a remote parent is dialled directly on Docker Desktop and Podman machine (VPN routes are usually reachable from their VMs). A host-loopback parent is `host.docker.internal:<port>` on all three runtimes. On Apple `container`, a remote parent goes through the host relay (Decision 9) as plain HTTP `host.docker.internal:<relay port>`; the relay re-originates TLS with the host's VPN routes and DNS.
- Spike 0.2 of `add-squid-egress-sidecar`: Squid 6.12 sends `CONNECT host:443` to the `cache_peer` for both spliced and bumped connections and never goes direct under `never_direct allow all`. A live check against a logging parent saw spliced, intercepted and plain-HTTP requests arrive as `CONNECT`/`GET`, and an unlisted host reset locally. A pin bump must re-run the parent-chaining e2e.
- Parent denials pass through unchanged and are attributed to the upstream proxy (a refused CONNECT or a 407).

### 8. Per-session sandbox inputs

The sudo step that runs the firewall resets the environment, so per-session inputs travel by file. `sessions/<sid>/sandbox/` is created under a flock while the session runs (stale ones are removed later) and mounted read-only at `/project-sandbox-egress`. It holds only:
- `ca.pem`, when interception is active;
- `upstream-ca.pem`, when an upstream CA is configured.

The `internet-proxy` firewall template gains a sidecar block, rendered only in sidecar mode, that installs these into `/usr/local/share/ca-certificates/` and runs `update-ca-certificates` before the rules are applied. The entrypoint's sidecar block exports `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `NODE_USE_SYSTEM_CA=1` when either file is present. The key is never placed here, and the sandbox image does not depend on either certificate.

### 9. Lifecycle under one lock

Host state, per runtime, in `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/` (0700):

```
egress/<runtime>/
├─ .lock                 serializes ensure / up / down / restart / reload / edits
├─ sidecar/              mounted read-only into the sidecar at /project-sandbox-egress
│  ├─ squid.conf  upstream-ca.pem
│  └─ ca.pem  ca-key.pem (0600)
├─ state.json            a cache: image, fingerprints, port, relay port, started_at
├─ relay.lock relay.pid relay.port relay.log
└─ sessions/<sid>/sandbox/
```

`state.json` is a cache, not the source of truth: every decision re-inspects the container, and a missing or garbled file means "needs up".

```
with path_lock("egress-sidecar", state_dir):
    c = inspect(project-sandbox-egress)
    if c is None or not running(c) or not ready(c) or sidecar/ incomplete:
        rm -f container                      # also clears a stopped one after a reboot
        check the port is free on 127.0.0.1  # clear error naming [egress.sidecar] port
        CA: keep or regenerate (Decision 3); render squid.conf and upstream CA
        relay if needed (below); run the sidecar with --publish 127.0.0.1:<port>:3128; wait_ready
    else:
        relay: respawn if its lock is free
        reload-only drift → reload; restart-only drift → warning
```

- **Readiness:** from inside the sidecar, `GET http://sidecar-readiness.invalid/` must return the `project-sandbox-egress: host-not-allowed` page within 30 s. BusyBox `nc` drops the connection when stdin ends, so the probe keeps stdin open for one second. On timeout the error includes the last 20 log lines. A launch additionally connects to the published port from the host before starting the sandbox.
- **Two fingerprints.** Reload: the rendered `squid.conf`, computed with the *running* upstream and Copilot setting so restart-only changes never show up as reloadable. Restart: image tag, Copilot flag, upstream URL and CA bytes, port, memory, CPUs, log options. The restart fingerprint is a container label; a difference produces a warning naming each field and `project-sandbox-egress restart`, and the launch continues on the running sidecar. An implicit restart would break every other running sandbox. Port drift is the exception that fails the launch (Decision 1).
- **Live reload:** write `sidecar/squid.conf` atomically (the directory is mounted, not the file), then `exec project-sandbox-egress sbx-reload`: copy, `squid -k parse`, `squid -k reconfigure`. Spike 0.3 of `global-egress-sidecar-macos`: a 60 s streaming CONNECT survived two reconfigures on Docker and one on Apple (42.8 MB in 60 s), and a host added live was tunnelled right after.
- **`down`:** kill the relay, `rm -f` the container, delete `sidecar/` (key included) and `state.json`. It does not check for users; running sandboxes lose egress. **`restart`:** `down` without deleting a reusable CA, then `up`.
- **Concurrency:** holding the lock across a cold start blocks a second launch until the sidecar is ready, which is what that launch needs. A test runs two `ensure()` threads and expects exactly one `run`.
- **A session's end** removes only its `sessions/<sid>/`; the sidecar keeps running.
- **Developer machines** that ran the squid-sidecar line may still have a `project-sandbox-egress` container and an `sbx-egress` network. `up` replaces a container whose state does not match, and `down` also removes an `sbx-egress` network when one is left; both are best effort.

**Relay (Apple, remote parent).** `python -m project_sandbox.egress_relay --state-dir … --port P` runs `BuildProxyForwarder` in the foreground. It holds an exclusive flock on `relay.lock` for its whole life, writes `relay.pid` and the bound `relay.port`, and logs to `relay.log`. It is spawned with `start_new_session=True` and detached stdio, so terminal hangups and the CLI's signal handling do not reach it.
- Liveness is "`relay.lock` is held", never a pid. `down` signals `relay.pid` only while the lock is held.
- A launch respawns a dead relay on the recorded port. If that port is taken, it binds port 0, records the new port, re-renders and reloads `squid.conf`; the restart fingerprint is unaffected.
- Spike 0.5 of `global-egress-sidecar-macos` (e2e `parent:relay-survives-first-session`): the relay kept serving session 2 after session 1's process group was hung up.
- *Alternatives rejected:* a relay in each CLI process (tunnels drop when the owner exits); a launchd agent (unnecessary, a launch respawns it).

### 10. Observability

- **Runtime logs** carry the access log. On Docker Desktop and Podman machine the sidecar runs with `--log-opt max-size=10m` (Docker also `max-file=3`). Apple `container` has no log options.
- **Session-end count:** read the sidecar's clock (`exec date +%s`) at session start and end, because VM clocks drift after sleep; count classified denials whose `%ts` falls in the window; print the count and `project-sandbox-egress log --denied --since <start>`, or nothing when there are none. Docker and Podman narrow the fetch with `logs --since` (widened by 60 s, then filtered by `%ts`). Apple `container logs` has no `--since` (only `-n` and `--follow`); spike 0.4 measured 0.02–0.04 s to fetch 50k lines, so it fetches everything.
- **Classification:** the `sbx_cause` note; `bump=terminate` rows count as `sni-mismatch`, failed peeks as `non-tls-tunnel`; readiness probes are ignored; attribution is `local policy` or `upstream proxy`.
- The count covers every sandbox and every other client of the port during the window; it is not per sandbox.

### 11. Resources

`--memory 256M --cpus 1` by default on every runtime (spike 0.1 of `global-egress-sidecar-macos`). `squid.conf` sets `cache_mem 8 MB` and `max_filedescriptors 8192`: without them Squid sizes its fd table from the runtime's `nofile` ulimit (about 1M on Docker Desktop) and sat at 166 MB RSS with a 186 MB peak. With them, `memory.peak` was 25 MB on Docker and 37 MB on Apple. Ten concurrent HTTPS requests through the sidecar all returned 200 in 0.07–0.19 s.

### 12. Sidecar image

- **Build context:** `sidecar_image.py` renders `templates/egress-sidecar/` (`Dockerfile`, `entrypoint.sh`, `sni-match`, `sbx-reload`, error pages) into `$XDG_STATE_HOME/project-sandbox/egress-sidecar-image/<hash>/`, never into a project. The tag is `project-sandbox-egress-sidecar:<first 16 hex of sha256 over the rendered files>`; an existing tag is reused.
- **Pins:** `alpine:3.22.1@sha256:…`, `squid~6.12` (apk's `~` accepts Alpine's security revisions; an exact `-rN` breaks once the branch drops it), `su-exec`, `openssl`. `scripts/update-pins.py` updates the Alpine digest and the Squid release together and keeps the `copilot-egress` fixture in sync. No `socat`: nothing forwards inside the sidecar any more.
- **Entrypoint (root):** stage `/project-sandbox-egress/{ca.pem,ca-key.pem,squid.conf,upstream-ca.pem}` into `/run/egress-sidecar` (`--tmpfs /run`), initialise the certgen database, chown to `squid` (key 0600), then `exec su-exec squid squid -N`, so **Squid is PID 1** and gets the stop signal. Squid running as `squid` from the start is required on Apple: one that drops from root fails with `FATAL: Cannot open '/dev/stdout' for writing`, because the console fd is root-owned; its output goes through a FIFO owned by `squid`, relayed by `cat` to the container log. The `no_suid: setuid(0)` warning from helpers is harmless.
- **Apple `container` 1.4.1 builder bug:** a `COPY` of a directory silently copies nothing, so error pages are copied file by file in a JSON-form `COPY`; a renderer test asserts every `COPY` source is a rendered file.
- `container stop` takes about 2 s and Squid exits normally.

### 13. Launch wiring

- `--egress firewall|sidecar`, default `firewall`; firewall-mode output stays byte-identical.
- On Docker Engine (`_docker_is_desktop` false) and rootless Podman on a Linux host (`_podman_uses_local_pasta`, or no Podman machine), sidecar mode is rejected before any container work until `egress-sidecar-linux`: loopback publishing alone does not reach their sandboxes.
- `_validate_egress_args` rejects, before any work and with replacement hints: `--no-firewall`, `--runtime chroot`, `--extra-domain` (`allow add`), `--internet-proxy` (`[egress.upstream] url`), `--ca-cert` (`[egress.upstream] ca_cert`), `--allow-github` (firewall mode), `--copilot-host` (the profile covers github.com accounts only).
- The policy is loaded and location-checked before any worktree exists. The firewall and entrypoint are rendered for the `internet-proxy` policy with the sidecar endpoint as the proxy destination and the sidecar CA block.
- Right before the sandbox runs: build or reuse the sidecar image, `ensure()`, check Copilot and the CA, write `sessions/<sid>/` on an `ExitStack`, start the local-service plans as in firewall mode, run the sandbox on the default network.
- Devcontainers stay firewall-only: a sidecar-mode launch neither renders nor modifies `.devcontainer/` and says so.
- **`--dry-run`** prints the policy path, effective allowlist, Copilot profile, intercepted hosts, CA constraints, the published endpoint and would-start/would-reuse plus drift, from a read-only `inspect` when the runtime is reachable. It writes no file, generates no key and creates no container.

### 14. `project-sandbox-egress`

- `egress_cli.py`, registered in `[project.scripts]`; argparse subparsers `up`, `down`, `restart [--new-ca]`, `status [--json]`, `reload`, `allow list|add|remove`, `log [--follow] [--denied] [--since TIME|DURATION]`. Every subcommand, nested ones included, takes `--runtime` and defaults to `container_cli.select_runtime()`.
- Exit codes: 0 success, 1 operational failure, 2 usage.
- `status` reports running and ready, the published endpoint, image currency, drift (each reload or restart field), CA constraints and expiry, relay state and the policy path, without creating, starting or removing anything.
- `project-sandbox doctor` reports the sidecar's status line; it is read-only by contract.

### 15. Verification probe set

Ported from `internet-proxy-locally` (commit `997bad78af6021e092aea0f62e76279f01db305d`, `src/internet_proxy_locally/checks/egress/`) and re-implemented in `scripts/e2e-egress-sidecar.py`, because upstream grades resets as inconclusive. Our grading:
- **CONNECT denial:** zero bytes then close or reset, plus a matching `sbx_cause` in the log; a `200` first is a fail (except the documented resolved-address case: TLS failure after `200`, `private-address` logged, no upstream connection).
- **Plain-HTTP and bumped denials:** a 403 whose body carries the expected `project-sandbox-egress:` label; any other ≥400 is an error.

Probes: allowed splice (plus an HTTP GET inside, log shows `splice`), unlisted host, IP literals (one table: loopback, RFC 1918, link-local, private IPv6, metadata; port 443 so the port rule does not mask it, plus one port-80 sample), metadata by name in direct mode only (`nip.io`/`sslip.io`), SNI mismatch (foreign, missing, bump-set host with foreign SNI, unlisted SNI, with a matching control), raw tunnel (plaintext and `SSH-2.0` against a splice and a bump host), ten concurrent CONNECTs, direct egress with proxy variables unset (public IPv4/IPv6, DNS, host addresses on non-local-service ports), GitHub repository probes, parent chaining with the intercepting `copilot-egress` fixture parent, a loopback-only host service through local-service forwarding, two concurrent sandboxes, reuse across launches, live `allow add`, `down` cutting sandboxes off, relay survival (Apple), and the session-end count. The Copilot session itself is covered by the billable `copilot-egress` suite. Not ported: `dns-rebinding`, `dns-mixed-answers`, `ptr-allowlist` (they need upstream's lab DNS fixture); left as TODO candidates.

Results of the squid-sidecar line, for comparison: all policy, parent and forward checks passed on Apple `container` 1.4.1, Docker Desktop 29.9.2 and rootless Podman 6.0.1 in a macOS machine (2026-11-05/06).

## Risks / Trade-offs

- **[The sandbox firewall is the only thing keeping a sandbox on the sidecar]** The sandbox has a route to the network, as in firewall mode. → The `internet-proxy` policy is default DROP with no DNS and fails closed when IPv6 cannot be enforced; the agent runs without root; `--no-firewall` is rejected. `docs/security.md` says that sidecar mode filters by hostname but does not add routing isolation over firewall mode.
- **[Any host process can use the sidecar]** It is on host loopback, and on Apple any container that reaches the localhost alias can reach it. → It only grants what `egress.toml` allows; the CA is never given to such clients. Documented.
- **[Denials are not attributable to a sandbox]** The client address is the runtime's port forwarder. → The session-end count says "across all sandboxes"; per-sandbox attribution stays on ROADMAP.
- **[The port is taken on the host]** → `up` checks before `run` and names `[egress.sidecar] port`.
- **[Changing the port rebuilds sandbox images]** It is baked into the firewall like any destination. → Rare; the restart warning says so.
- **[Apple `container` loopback publishing]** → Verified by spike 0.1 on all three runtimes (Decision 1); the fallback was not needed.
- **[CONNECT denials are resets, not pages]** → The cause is in the log and the session-end count; documented.
- **[Hostname-only policy in parent mode]** loses IP-level SSRF protection and lets allowlisted intranet names through the parent. → Strict validation, no IP literals, documented.
- **[The CA key lives up to 30 days on the host]** → 0600 in a 0700 directory, never mounted into a sandbox (checked every launch), constrained to `github.com` or `invalid`, deleted by `down`.
- **[`down`/`restart` break running sandboxes]** → Accepted by design; documented.
- **[A hand edit of `egress.toml` widens the policy for running sandboxes at the next launch]** → Intended (one control point); the launch prints what it reloaded.
- **[GitHub `git`/`gh` fail in sidecar sessions]** → Intended; firewall mode with `--allow-github` remains, per-repository access is on ROADMAP.

## Migration Plan

- Opt-in: firewall mode and `--internet-proxy` without `--egress sidecar` are unchanged. Rollback means omitting `--egress sidecar`, then `project-sandbox-egress down`.
- No released version had a sidecar. Developer machines that ran the squid-sidecar line are handled as described in Decision 9.

## Open Questions

- Exact `--log-opt` sizes and warning wording can be tuned during implementation.
