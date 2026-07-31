# Implementation Plan: agentgateway LLM Sidecar for `project-sandbox`

## Primary goal
**No API keys and no OAuth credentials ever enter the agent VM.** The gateway exists to make credential staging unnecessary: it holds the upstream provider credential, the agent holds a per-session sentinel token that is worthless outside the sandbox network and is revoked at teardown. Every design decision below is subordinate to this goal — where a feature (subscription passthrough, GitHub-hosted Copilot) cannot be delivered without a credential in the agent VM, the plan refuses it rather than weakening the invariant.

## TL;DR
- Add an opt-in `--gateway` feature that launches **agentgateway** (`ghcr.io/agentgateway/agentgateway:latest`, multi-arch incl. linux/arm64) as a second Apple `container` VM on a per-project user-defined network, forces the agent VM's LLM traffic through it, and injects provider API keys at the proxy so the agent VM never holds them — mirroring the existing `--pi-ollama` forwarding shape and the ROADMAP's mitmproxy sidecar spec.
- **`--gateway on` implies `--no-stage-credentials`** and makes it non-overridable: the credential-staging step is skipped entirely, a preflight assertion fails the run if any known credential path would be (or has been) placed in the agent home, and each agent is configured with a sentinel bearer token instead of a real one.
- Config is a generated, validated agentgateway YAML (`binds → listeners → routes → backends: ai`) rendered by a new jinja2 template into `.project-sandbox/gateway/config.yaml`, with per-agent base-URL env injection (`ANTHROPIC_BASE_URL`, Codex `config.toml` custom provider, `COPILOT_PROVIDER_BASE_URL`); requires macOS 26 for inter-container networking.
- Secrets live in one host `secrets.env` (0600) mounted read-only into the gateway VM **only**, with an opt-in macOS Keychain path via `security`/`keyring`; the agent VM gets base-URL config but no provider keys, and keys are referenced in gateway config as `$ENV` so they never land in git, images, or logs.

## Key Findings

### agentgateway
- **What it is:** an open-source Rust-based "agentic proxy" from the agentgateway org (Solo.io / kgateway ecosystem). **Agentgateway is a Linux Foundation project** (per the repo README); its 1st-birthday blog (agentgateway.dev, 2026‑03‑25) notes "1 million pulls, 175+ contributors, and 2,000+ stars in 1 year," and the repo now shows **4,038 stars, Apache‑2.0, 672 forks** (GitHub org page; the issues page shows "Star 4.1k" as of Jul 30, 2026). It is an AI-native data plane providing an LLM gateway (OpenAI-compatible unified API, failover, budgets), an MCP gateway (tool federation/multiplexing), and A2A support. GitHub: `github.com/agentgateway/agentgateway`.
- **Config schema:** JSON or YAML. Top-level sections: `config` (static top-level settings, e.g. `adminAddr`, `logging`), `binds` (the full `listeners → routes → backends` routing model), plus simplified `llm` and `mcp` top-level modes. An LLM route uses `backends: - ai: { name, provider: { openAI/anthropic/gemini/bedrock/ollama: {...} } }` with `policies.backendAuth.key: "$OPENAI_API_KEY"`. Most file changes hot-reload automatically (except the top-level `config` section); the UI overwrites the file.
- **Credentials:** four options — inline (least secure), **environment variable** (`key: "$OPENAI_API_KEY"`, auto-substituted from the proxy process env), **file** (loaded into an env var), and Kubernetes secret / passthrough. Also `auth.passthrough: {}` forwards the client's incoming Authorization token upstream unchanged, and `auth.aws` signs with SigV4.
- **Container image:** published to GHCR at `ghcr.io/agentgateway/agentgateway:latest`; Dockerfile builds both `linux/amd64` and `linux/arm64` via `TARGETARCH` on a Chainguard glibc-dynamic base — so an arm64 image suitable for Apple Silicon exists. Standard run: `docker run -p 3000:3000 -p 15000:15000 -v $(pwd)/config.yaml:/config.yaml ghcr.io/agentgateway/agentgateway:latest -f /config.yaml`.
- **Ports:** data/proxy listener default `3000` (examples also use 4000/4001); admin UI `15000`; Prometheus metrics on `15020` — per the agentgateway Prometheus docs, "Agentgateway exposes metrics on port 15020 by default," with the token metric named `agentgateway_gen_ai_client_token_usage`.
- **Documented coding-agent use case:** agentgateway's own docs and a widely-cited walkthrough (Sebastian Maniak, maniak.io) show routing Claude Code, Codex, and OpenCode through it via `ANTHROPIC_BASE_URL=http://localhost:4001/claude` and a Codex `model_provider` pointing at `http://localhost:4001/codex/v1`, with either subscription passthrough or `backendAuth`-injected API keys. This is precisely the sidecar pattern we want.

### How `project-sandbox` currently does "ollama forwarding" (the pattern to mirror)
- **Module layout** (`AGENTS.md`): source in `src/project_sandbox/`; CLI entry `cli.py`; runtime command construction `container_cli.py`; generated assets rendered from `src/project_sandbox/templates/` via jinja2 (the only runtime dependency). Firewall logic in `firewall.py`; build fingerprinting in `build_cache.py`. Tests in `tests/` (`test_cli.py`, `test_renderers.py`, `test_container_cli.py`).
- **Runtime adapter:** `container_cli.py` defines a `Runtime` abstraction with a `_RUNTIMES` registry, `RUNTIME_CHOICES`, and `select_runtime()` (auto-selects Apple `container` on macOS, docker→podman on Linux; validates the binary with `shutil.which`). All three current runtimes share one argv builder, `build_run_argv()` (`run --rm --memory … --mount type=bind … <image> <cmd>`), executed via `container_cli.run(cmd, dry_run=...)`. There is `ensure_system_started()`, `image_exists()`, `build_image()`.
- **The ollama feature (`--pi-ollama` / `--ollama-model`, used with `--agent pi`):** per `docs/security.md`, when `--pi-ollama` is set the firewall (both CLI and devcontainer variants of `init-firewall.sh`) additionally allows outbound TCP to a **runtime-adapter-selected host endpoint restricted to port 11434**, and it "bakes Pi's provider config to use it." Ollama stays on host loopback. The host endpoint is resolved differently per runtime: **Apple `container`** requires the user to preconfigure localhost DNS (project-sandbox never calls `sudo`); **Linux bridge (docker/podman)** modes conditionally require **`socat`**, bound to the exact validated bridge address and torn down with the sandbox. `--ollama-model` overrides the default model list. This is a host-forwarding (not sidecar) design, but it establishes the config keys / firewall / provider-config-baking pattern to follow.
- **Existing sidecar prior art in-repo:** none shipped, but `ROADMAP.md` contains a fully worked **"Credential-Filtering Sidecar Proxy"** spec (mitmproxy in a second Apple `container` VM). It is the direct architectural template for this feature and already solves the hard problems: `container network create --subnet 192.168.65.0/24 proxy-net-<projectid>`; because Apple `container` cannot pin IPs, read the sidecar IP post-start via `container inspect` and template it into `init-firewall.sh` at container start; require **macOS 26** for inter-container networking; agent VM iptables locked to ALLOW only the sidecar IP:port (default DROP, fail-closed); `wait-for-proxy.sh` gate (exit 64 on timeout); devcontainer sidecars via a generated `.devcontainer/docker-compose.yml` with `depends_on: {proxy: {condition: service_healthy}}` and `runServices: [agent, proxy]`; single host `secrets.env` (0600) mounted RO into the proxy VM only, with `--keychain` reading values via `security find-generic-password -s project-sandbox.<key> -w`.

### How the coding agents can be pointed at a gateway base URL
- **Claude Code:** honors `ANTHROPIC_BASE_URL` (route target) and `ANTHROPIC_AUTH_TOKEN` (Bearer for the gateway), plus optional `ANTHROPIC_MODEL`; settable in `~/.claude/settings.json` `env` block or shell. `ANTHROPIC_API_KEY` is the direct key; `ANTHROPIC_AUTH_TOKEN` is for gateways/proxies.
- **Codex CLI:** define a custom provider in `~/.codex/config.toml`: `[model_providers.<id>]` with `base_url`, `env_key`, `wire_api` ("chat" or "responses"); or set `openai_base_url` for the built-in openai provider. Project-local `.codex/config.toml` cannot override provider auth/definitions (security boundary) — provider definitions must be in user config.
- **OpenCode:** supports custom OpenAI-compatible providers with a `baseURL` in its config (`~/.config/opencode`).
- **GitHub Copilot CLI:** uses `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_API_KEY`, `COPILOT_MODEL` (BYOK, OpenAI-compatible Chat Completions). Note: Copilot CLI does **not** honor `OPENAI_BASE_URL` and does not reliably route via `HTTPS_PROXY`. Per GitHub issue #2283: "Copilot CLI cannot be routed through OpenAI-compatible proxies… because it uses proprietary GitHub endpoints and ignores standard environment variables like OPENAI_BASE_URL… HTTPS_PROXY does not reliably intercept model traffic, so there is currently no clean way to redirect requests." Only the `COPILOT_PROVIDER_*` BYOK path works, and only for custom/local models — not GitHub-hosted Copilot models.

### Apple `container` networking (relevant constraints)
- Container-to-container networking requires **macOS 26 (Tahoe)**; on macOS 15 each container gets its own VM but siblings cannot talk directly (port publishing still works). Each container gets its own IP on the default `192.168.64.0/24` bridge — confirmed: "The container's IP address is on a private network (192.168.64.0/24) and is only reachable from your Mac."
- **No explicit IP pinning yet** — per Apple container issue #282: "containers launched via the container CLI do not offer a way to define or control their IP address… While `--network` allows attaching to a custom network, it doesn't currently support IP address assignment or any advanced networking options." Discover IPs via `container inspect`. Verified syntax (Suraj Deshmukh, suraj.io, 2026): `container inspect web | jq '.[0].networks[0].ipv4Address'` returns e.g. `"192.168.64.9/24"` — note the leading `.[0]` array index (the CLI returns a JSON array).
- Inter-container DNS by name needs `sudo container system dns create <domain>` + `container system property set dns.domain <domain>` (writes `/etc/resolver/`, requires admin, and creating a localhost domain disables iCloud Private Relay and is dropped on reboot). `container run --dns-domain <domain>` gives containers dotted names.
- DNS can hiccup after sleep/wake (`container system stop && start` resets).

### Secure secret handling on macOS
- **`keyring`** (Python) uses the macOS Keychain natively; caveat: any Python script from the same executable can read secrets without a prompt unless Access Control is tightened. **`security find-generic-password -s <svc> -w`** is the zero-dependency shell equivalent.
- **`.env` file at 0600** (dir 0700) is the pragmatic single source of truth (dotenv format the agents already consume); never commit it (already covered by project-sandbox's `.gitignore` safeguards) and never bake secrets into an image layer.
- project-sandbox's existing stance (`AGENTS.md`, `docs/security.md`): "Avoid passing secrets through environment variables; prefer mounted credential files." Apple `container` logs the full process environment to `vminitd.log`, so env-based secrets can leak — another reason to keep keys in the gateway VM only.

## Details

### Proposed design

#### CLI surface (argparse, consistent with existing conventions)
Mirror the `--pi-ollama` / `--proxy` shape:
```
--gateway[=auto|on|off]        Run an agentgateway LLM sidecar. Default 'auto' if
                               .project-sandbox/gateway/ exists, else 'off'. 'on'
                               fails if the sidecar can't start.
--gateway-image TAG            Override sidecar image (default
                               ghcr.io/agentgateway/agentgateway:<pinned-digest>).
--gateway-provider NAME        Repeatable. Which upstream providers to route
                               (anthropic, openai, gemini, bedrock, ollama, ...).
--gateway-env-file PATH        Secrets file (default ~/.config/project-sandbox/secrets.env).
--gateway-key HOST=ENV_KEY     One-line credential mapping override. Repeatable.
--gateway-config PATH          Override generated config.yaml path.
--gateway-keychain             Assemble secrets.env from macOS Keychain each run.
--gateway-admin                Publish the admin UI (15000) / metrics (15020) to host loopback.
```
Defaults and dry-run behavior must match existing flags: dry-run validates and prints the `container network create` / `container run` / templated config argv, redacts secret values as `<redacted>`, and writes/starts nothing.

#### Config schema additions (project config)
Add a `gateway` block resolvable through the existing project config mechanism (persisted under `.project-sandbox/`), e.g.:
```yaml
gateway:
  enabled: false
  image: ghcr.io/agentgateway/agentgateway@sha256:<pinned>
  listen_port: 3000
  admin: false                # publish 15000/15020 to host loopback
  providers:                  # maps to generated agentgateway routes/backends
    - name: anthropic
      route_prefix: /claude
      secret: ANTHROPIC_API_KEY
    - name: openai
      route_prefix: /codex
      secret: OPENAI_API_KEY
  env_file: ~/.config/project-sandbox/secrets.env
  keychain: false
```
The CLI partitions `secrets.env` into **config** vars (passed into the agent VM) and **secret** vars (held back; used only to render the gateway config's `$ENV` references and injected into the gateway VM env), exactly as the ROADMAP proxy design partitions its `.env`.

#### Generated agentgateway config template
New `src/project_sandbox/templates/agentgateway-config.yaml.j2` rendering a routing-based config (one route per enabled provider), following the Maniak / agentgateway Claude Code patterns:
```yaml
# yaml-language-server: $schema=https://agentgateway.dev/schema/config
config:
  adminAddr: 0.0.0.0:15000
  logging: { level: info, format: json }
binds:
- port: {{ listen_port }}
  listeners:
  - name: default
    protocol: HTTP
    routes:
    - name: claude-agent
      matches: [ { path: { pathPrefix: /claude } } ]
      policies:
        urlRewrite: { path: { prefix: / } }
        backendAuth: { key: "$ANTHROPIC_API_KEY" }
      backends:
      - ai: { name: claude-agent, provider: { anthropic: {} } }
      policies:
        ai:
          routes:
            /v1/messages: messages
            /v1/messages/count_tokens: anthropicTokenCount
            '*': passthrough
    - name: codex-agent
      matches: [ { path: { pathPrefix: /codex } } ]
      policies:
        urlRewrite: { path: { prefix: / } }
        backendAuth: { key: "$OPENAI_API_KEY" }
      backends:
      - ai: { name: codex-agent, provider: { openAI: {} } }
      policies:
        ai:
          routes:
            /v1/chat/completions: completions
            /v1/responses: responses
            '*': passthrough
```
Validation: parse with `yaml.safe_load` and check required keys before mounting; optionally validate against agentgateway's published JSON schema.

#### Lifecycle / orchestration logic
Add a `Runtime`-aware orchestrator in `container_cli.py` (new `build_gateway_run_argv()` and helpers), invoked from `cli.py` when `--gateway` is on, following the ROADMAP sequence exactly:
1. Read/partition `secrets.env`; render `config.yaml` (with `$ENV` references only, no literal secrets).
2. `container network create --subnet 192.168.65.0/24 gateway-net-<projectid>` (Apple runtime).
3. `container run -d --name gateway-<projectid> --network gateway-net-<projectid> --mount type=bind,source=<config.yaml>,target=/config.yaml,readonly --mount type=bind,source=<secrets.env>,target=/run/secrets.env,readonly <image> -f /config.yaml` (secrets env loaded into the proxy process; keys never mounted into the agent VM).
4. Poll for the gateway IP via `container inspect gateway-<projectid> | jq '.[0].networks[0].ipv4Address'` (strip the `/24` suffix); health-check the listener (TCP connect to `:3000`, optionally `GET /` or metrics `:15020`).
5. Template the agent VM's `init-firewall.sh` with `GATEWAY_IP`, then build/run the agent VM on the same network with base-URL env injected (below).
6. On teardown, stop both containers and `container network delete` the per-project network; a failed session is left in place for inspection (consistent with `--keep-workspace` semantics).

For docker/podman on Linux, reuse the existing shared bridge / `socat` pattern from the ollama feature, or run the gateway as a peer container on a user-defined bridge (Docker/Podman give name-based DNS on custom networks). Keep this in an isolated code path like the ROADMAP's `docker-sandbox` runtime.

#### Container networking plan
- **Apple `container` (macOS 26+):** shared user-defined network; agent reaches gateway at the inspected IP, or by name via `container system dns create project-sandbox.local` so `gateway.project-sandbox.local` resolves (optional, requires admin + disables Private Relay — keep it opt-in, default to IP templating). Hard-fail on macOS 15 with a clear message when `--gateway` is set.
- **Reachability from agent:** base URL becomes `http://<GATEWAY_IP>:3000/claude` etc. `NO_PROXY=localhost,127.0.0.1`.
- **Docker/Podman:** custom bridge with automatic inter-container DNS; agent uses `http://gateway-<projectid>:3000/...`.

#### Firewall / allowlist interaction
Extend `firewall.py` + `init-firewall.sh.j2` so that when `--gateway` is set the agent VM's egress allowlist collapses to a **single destination**: ALLOW `GATEWAY_IP:3000` (TCP), default DROP for everything else (fail-closed if the gateway dies). The gateway VM keeps a coarse domain-pinned allowlist (the existing curated set: `api.anthropic.com`, `api.openai.com`, `auth.openai.com`, `chatgpt.com`, plus `--allow-github`/`--extra-domain` and any enabled provider hosts) so only the sidecar can reach LLM provider endpoints. Refactor the curated domains into `firewall.base_agent_domains(*, allow_github)` (already proposed in ROADMAP) so the iptables script and the gateway's allowlist share one source of truth. Block QUIC (`udp --dport 443 REJECT`) so clients use TCP that the proxy can see.

#### Agent env-var injection
Injected into the agent VM (config, not secrets):
- **Claude:** `ANTHROPIC_BASE_URL=http://<GATEWAY_IP>:3000/claude`, `ANTHROPIC_AUTH_TOKEN=<sentinel>` (gateway injects the real key). Written into the generated `.project-sandbox/claude/settings.json` `env` block (mirrors existing claude/codex config generation).
- **Codex:** generated `.project-sandbox/codex/config.toml` custom provider `base_url=http://<GATEWAY_IP>:3000/codex/v1`, `wire_api="responses"` (or "chat"), `env_key` pointing at a sentinel; keep provider defs in the generated user-level config since project-local Codex config cannot set provider auth.
- **OpenCode:** custom provider `baseURL` in generated config.
- **Copilot CLI:** `COPILOT_PROVIDER_BASE_URL=http://<GATEWAY_IP>:3000/...`, `COPILOT_MODEL`, sentinel `COPILOT_PROVIDER_API_KEY` (only for BYOK/custom-model use; GitHub-hosted Copilot cannot be routed and should print a warning).

### Secure secrets design (recommended + rejected)
**Recommended:** single host `secrets.env` at `~/.config/project-sandbox/secrets.env` (mode 0600, dir 0700), overridable per-project (`.project-sandbox/secrets.env`) or `--gateway-env-file`. Keys are referenced in the generated agentgateway config as `$VARNAME` (agentgateway's env substitution) and the file/env is provided to the **gateway VM only** (bind-mount RO at `/run/secrets.env` and/or `-e` into the gateway process). The agent VM receives only base-URL config and sentinel tokens. Dry-run redacts values. Opt-in `--gateway-keychain` assembles the env in-memory from macOS Keychain via `security find-generic-password -s project-sandbox.<key> -w` (or the `keyring` library) and writes it to a tmpfs-backed file mounted RO into the gateway VM, so nothing sits in plaintext at rest. Log redaction: the gateway runs with JSON logging and never logs bodies; project-sandbox logs one line per request without keys; sentinel exact-match tripwire optional.

**Rejected alternatives:**
- *Env vars into the agent VM* — rejected: Apple `container` logs the full env to `vminitd.log`; agent code / malicious npm hooks can read `/proc/self/environ`.
- *Baking keys into the image or config file committed to git* — rejected: violates the no-secret-in-image / no-secret-in-repo rule; images are exfiltration targets.
- *Inline keys in agentgateway config* — rejected by agentgateway's own docs as "least secure."
- *sops/age or 1Password CLI as the primary store* — deferred: heavier dependencies; can be layered later as an alternate `--gateway-*` source, but `.env` + Keychain covers the macOS-first use case with minimal deps (project-sandbox's minimal-dependency ethos).
- *Passthrough auth (agent holds the real key, gateway forwards it)* — rejected as the default because it defeats the non-possession goal; offered only for subscription-passthrough mode where no API key exists.

### Phased implementation plan (TODO.md-driven)

**Phase 0 — Spec & guardrails**
- Add `docs/gateway.md` and a `ROADMAP.md`/`TODO.md` entry. Acceptance: doc merged; macOS-26 requirement and threat model written.

**Phase 1 — Config generation (no runtime)**
- New `agentgateway-config.yaml.j2` + renderer; `gateway` config block parsing; `secrets.env` partitioning; sentinel injection. Acceptance: `--dry-run --gateway on` renders a schema-valid config referencing `$ENV` only, redacts secrets, writes nothing; unit tests in `test_renderers.py`.

**Phase 2 — Apple `container` orchestration**
- `build_gateway_run_argv()`, network create/inspect/health-check/teardown; `init-firewall.sh` `GATEWAY_IP` templating (single-destination allow, fail-closed); macOS-15 hard-fail. Acceptance: on macOS 26, `--gateway on --agent claude` starts both VMs, agent reaches gateway, direct egress DROPs; argv unit tests mock `shutil.which`/`container inspect`.

**Phase 3 — Agent wiring**
- Base-URL env / config generation for Claude, Codex, OpenCode, Copilot; sentinel token handling. Acceptance: E2E — `claude --print` completes through the gateway; `env | grep -i ANTHROPIC_API_KEY` inside agent VM shows only the sentinel; no `sk-ant-` on the agent VM filesystem/`/proc`.

**Phase 4 — Secrets ergonomics**
- `--gateway-keychain` via `security`/`keyring`; per-invocation ephemeral secrets; log redaction/tripwire. Acceptance: Keychain path assembles env with no plaintext at rest; redaction test passes.

**Phase 5 — Docker/Podman + devcontainer**
- Linux bridge path (reuse ollama socat/native pattern) and generated `.devcontainer/docker-compose.yml` (`agent`+`gateway`, `depends_on: service_healthy`, `runServices`). Acceptance: dry-run prints correct argv/compose; interactive Linux run works.

**Phase 6 — Observability & polish (stretch)**
- Optional publish of admin UI (15000) / metrics (15020) to host loopback; token-cost dashboard docs (the `agentgateway_gen_ai_client_token_usage` metric); MCP-gateway multiplexing as a follow-on. Acceptance: `--gateway-admin` exposes UI/metrics on loopback only.

### Testing strategy
- **Unit:** argv construction (`build_gateway_run_argv`, network/inspect commands), config rendering (schema-valid, `$ENV`-only, redaction), `firewall.base_agent_domains`, macOS-15 rejection, dry-run writes nothing. Follow existing render-only/argv-inspection conventions; mock `sys.platform`, `shutil.which`, `container inspect`.
- **Integration (macOS 26 host):** two-VM start, IP discovery, health gate, single-destination firewall, teardown/network delete; `test_raw_secret_never_in_agent_vm`, `test_legitimate_request_works`, `test_fail_closed_on_gateway_crash`, `test_dns_tunnel_blocked`, `test_prompt_injection_workspace_file` (adapted from ROADMAP's suite).
- **Manual smoke:** `--dry-run` argv review; real Claude/Codex/Copilot session; kill the gateway mid-session and confirm the agent fails closed within ~2s; confirm metrics on `:15020`.

### Failure modes / rollback
- Gateway fails to start / health-check times out → with `--gateway on`, abort with a clear error; `wait-for-proxy.sh` gate exits 64 and propagates. `auto` falls back to `off` only if no `gateway/` dir.
- Gateway crashes mid-session → agent VM iptables single-destination rule means fail-closed (no direct egress); CLI-managed restart-pair since Apple `container` can't pin IPs (a gateway restart requires an agent restart).
- Config invalid → caught at render/validate before any container starts.
- macOS 15 → hard-fail. DNS hiccup after sleep/wake → documented `container system stop && start` remedy.
- Rollback: feature is fully opt-in; omitting `--gateway` restores current behavior; `.project-sandbox/gateway/` can be deleted.

## Recommendations
1. **Build the Apple-`container` two-VM path first** (Phases 1–3), reusing the ROADMAP mitmproxy sidecar spec's mechanics verbatim (network create, `container inspect | jq '.[0].networks[0].ipv4Address'` IP discovery, `GATEWAY_IP`-templated single-destination firewall, `wait-for-proxy.sh`). This is the highest-value, best-sourced path and matches the repo's macOS-first posture.
2. **Adopt the routing-based agentgateway config** (`binds/listeners/routes/backends: ai` with per-provider `backendAuth.key: "$ENV"`), not the simplified `llm` mode, because per-route path prefixes (`/claude`, `/codex`) map cleanly onto each agent's base-URL env var and the documented Maniak pattern.
3. **Keep secrets in the gateway VM only**, referenced as `$ENV` in config, sourced from `secrets.env` (0600) with an opt-in Keychain path; never inject provider keys into the agent VM.
4. **Pin the image by digest** and expose `--gateway-image`; verify the `arm64` manifest at build time.
5. **Warn on unsupportable cases:** GitHub-hosted Copilot (only BYOK routable per issue #2283), subscription-passthrough (agent holds no key so no injection), and any `container system dns` use (disables Private Relay).

**Thresholds that change the plan:**
- If Apple `container` inter-container networking is too buggy on the target macOS 26.x → fall back to a single combined VM running the gateway under a separate uid excluded from the iptables redirect (weaker isolation, unblocks shipping).
- If an agent adopts TLS certificate pinning → the base-URL redirect still works (it's not MITM; the agent talks plain HTTP to the gateway which terminates its own TLS upstream), so pinning is **less** of a problem here than in the mitmproxy design — a point in favor of the base-URL approach over transparent MITM.
- If per-project VM count causes memory pressure (~4 concurrent agents on 16 GB) → add a `--shared-gateway` mode (one gateway serving multiple agent VMs), accepting cross-project policy coupling.

## Caveats
- **Exact `--pi-ollama` code (line-level) unverified:** GitHub blocks automated raw-source fetches, so flag defaults/help strings, the `init-firewall.sh.j2` ollama iptables line, the socat invocation, and the Pi provider-config writer function name are inferred from `docs/security.md`, `docs/usage.md`, `AGENTS.md`, and `ROADMAP.md` rather than read directly from `cli.py`/`container_cli.py`/`firewall.py`. Confirm names against the tree before coding.
- **agentgateway is pre-1.0 / actively developed** — config schema (e.g. `binds` vs simplified `llm`, `hostOverride`/`pathPrefix` for Codex's chatgpt.com backend) may shift between releases; pin the image digest and re-validate the generated config against the version in use.
- **Codex-through-ChatGPT-subscription** requires `hostOverride: chatgpt.com:443` + `pathPrefix: /backend-api/codex` and `requires_openai_auth = true`; this is subscription passthrough (agent holds credentials), which is at odds with the non-possession goal — treat as a distinct, documented mode.
- **macOS 26 is a hard requirement** for the sidecar; `container system dns` changes packet-filter state and disables iCloud Private Relay and must be admin-initiated.
- **Copilot CLI cannot route GitHub-hosted models** through any proxy (open issue #2283); only BYOK/OpenAI-compatible models via `COPILOT_PROVIDER_*` work.

### Key sources
- agentgateway: https://agentgateway.dev/ , https://github.com/agentgateway/agentgateway , config overview https://agentgateway.dev/docs/standalone/latest/configuration/overview/ , LLM providers https://agentgateway.dev/docs/standalone/latest/llm/providers/ , API keys https://agentgateway.dev/docs/llm/api-keys/ , Claude Code integration https://agentgateway.dev/docs/standalone/main/integrations/llm-clients/claude-code/ , installation/arch https://www.mintlify.com/agentgateway/agentgateway/installation , metrics https://agentgateway.dev/docs/standalone/main/integrations/observability/prometheus/
- Coding agents through agentgateway: https://maniak.io/articles/2026-05-08-claude-codex-passthrough-through-agentgateway/
- project-sandbox: https://github.com/pkrusche/project-sandbox , AGENTS.md, docs/security.md, docs/runtime.md, docs/usage.md, ROADMAP.md, TODO.md
- Apple container networking: https://github.com/apple/container/blob/main/docs/how-to.md , https://www.mintlify.com/apple/container/guides/networking , issue #282 (IP pinning), issue #402 (DNS)
- Agent env vars: Claude https://www.requesty.ai/blog/claude-code-environment-variables-anthropic-base-url-auth-token ; Codex https://developers.openai.com/codex/config-advanced ; Copilot https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models and issue https://github.com/github/copilot-cli/issues/2283
- Secrets: https://keyring.readthedocs.io/
