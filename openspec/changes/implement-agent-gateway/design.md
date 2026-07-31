## Context

`project-sandbox` currently has two ways an agent VM gets LLM credentials:
forwarded host OAuth credentials (`config_agents.sync_credentials`, disabled
by `--no-forward-credentials`) and staged API keys (`--api-key-env` /
`--api-key-env-file`, which requires `--no-forward-credentials`). Both place a
live, usable credential inside the agent VM for the session's duration. This
change adds a third path — an `agentgateway` sidecar VM that holds the
credential instead — without touching the other two.

Two corrections to source material worth recording here since they shaped
several decisions below:
- `ROADMAP.md` does **not** currently contain a "Credential-Filtering Sidecar
  Proxy" / mitmproxy design (it currently covers only prebuilt-image licensing
  review). There is no existing in-repo sidecar prior art to reuse; the
  network-create/inspect/health-check/teardown mechanics below are new, though
  they follow the same shape as `ollama_network.py`'s `ForwardingPlan`.
- The credential-forwarding flag is `--no-forward-credentials` (see
  `cli.py`), not `--no-stage-credentials`.
- The devcontainer generator (`devcontainer.py`) currently emits a single
  `devcontainer.json` with no `docker-compose.yml` — there is no existing
  multi-service devcontainer path to extend. Adding one is out of scope here.

The closest in-repo analog for "reach a host/sidecar service from the agent
VM under a runtime-specific strategy, with a firewall allowlist entry and
baked provider config" is `ollama_network.py` + `firewall.render(...,
pi_ollama=...)`. This design follows that module's shape (a small
`prepare()`/lifecycle object per runtime) rather than inventing a new
pattern.

## Goals / Non-Goals

**Goals:**
- No API key or OAuth credential is ever staged, mounted, or set as an
  environment variable inside the agent VM when the gateway is active.
- Reuse the existing runtime abstraction (`container_cli.Runtime`,
  `_RUNTIMES`, `build_run_argv`) and the existing template-rendering
  convention (`templating.py` + `src/project_sandbox/templates/`) rather than
  introducing new mechanisms.
- Fail closed at every stage: preflight credential tripwire, config
  validation before any container starts, health-gated agent VM start,
  single-destination firewall so a dead/compromised gateway can't be
  bypassed.
- Support the three agents whose CLIs accept a redirectable base URL and
  auth token without a proprietary transport (Claude Code, Codex CLI,
  OpenCode).

**Non-Goals (this change):**
- macOS Keychain-backed secrets assembly (`--gateway-keychain`). The
  env-file path (0600, partitioned, redacted in dry-run) is sufficient to
  satisfy the credential-isolation goal; Keychain is an ergonomics layer that
  can be added later without touching the spec's requirements.
- Docker/Podman peer-container support. Apple `container` is the only
  runtime targeted; Linux runtimes keep their current direct-credential
  behavior when `--gateway` is requested (rejected per the "Unsupported
  platform" scenario).
- Devcontainer sidecar support. `devcontainer.py` has no compose-based
  multi-service path today; building one is a separately scoped change.
- Publishing the gateway's admin UI or Prometheus metrics port to the host.
- GitHub-hosted Copilot routing — upstream Copilot CLI does not honor a
  redirectable base URL for GitHub-hosted models (tracked upstream as
  github/copilot-cli#2283); only BYOK/custom-model Copilot configurations
  could ever be routed, and that path is also deferred.
- Subscription-passthrough modes (e.g., ChatGPT-subscription-backed Codex)
  where the agent would need to hold a real credential — structurally
  incompatible with this feature's primary invariant, not just deferred.

## Decisions

**Two-VM sidecar over transparent MITM proxy.** Point each agent at the
gateway via its native base-URL environment variable / config field, rather
than transparently intercepting TLS traffic (which was the shape of the
never-built ROADMAP mitmproxy idea). Rationale: every agent this change
supports already has a documented, first-class "custom provider / base URL"
mechanism, so a redirect requires no certificate trust changes in the agent
VM and is not defeated by certificate pinning — the agent VM talks plain
HTTP to the gateway, which terminates TLS upstream itself.

**Routing-based agentgateway config (`binds/listeners/routes/backends: ai`)
over the simplified top-level `llm` mode.** Per-route path prefixes
(`/claude`, `/codex`, `/opencode`) map directly onto each agent's base-URL
env var, matching the documented Claude-Code-through-agentgateway pattern.
The simplified `llm` mode does not offer per-agent path routing.

**Sentinel bearer token, not passthrough auth.** The gateway injects the real
provider credential via `policies.backendAuth.key: "$ENV_VAR"`; the agent
holds a fixed, non-secret sentinel value the gateway does not validate as a
real key. Passthrough auth (agent holds the real key, gateway just forwards
it) is rejected as a default because it defeats the primary goal; it is not
built at all in this change since none of the three in-scope agents need it.

**Single-destination firewall collapse over a per-provider-domain allowlist
in the agent VM.** When the gateway is active, the agent VM's egress
allowlist becomes exactly `GATEWAY_IP:port` with default DROP — modeled on
`--pi-ollama`'s port-scoped ACCEPT rule, but collapsed to one destination
since every provider request now goes through the same sidecar. The gateway
VM keeps the broader provider-domain allowlist instead, since it is the only
thing that needs to reach `api.anthropic.com` / `api.openai.com` directly.

**Apple `container` network create + `container inspect` IP discovery, no
DNS.** Apple `container` cannot pin IPs (apple/container#282); the gateway's
address is discovered post-start via `container inspect ... | jq
'.[0].networks[0].ipv4Address'` and templated into the agent VM's firewall
script and base-URL config at start time, the same shape `ollama_network.py`
already uses for its `apple-preconfigured-localhost-dns` case (there, the
name is preconfigured; here, the network is per-project and created by this
feature, so no `sudo container system dns create` is needed or used).

**Hard macOS-26 gate, not a degraded fallback.** Apple `container`
inter-container networking does not exist before macOS 26; rather than
attempt a same-VM degraded mode, `--gateway on` fails outright on macOS 15
with a message naming the requirement. A same-VM fallback (gateway process
under a separate uid in the agent VM) would put the credential back in the
agent VM's filesystem/process tree, which conflicts with the primary goal —
so it is rejected outright rather than deferred.

## Risks / Trade-offs

- **agentgateway is pre-1.0** and its config schema may shift between
  releases → pin the image by digest (`--gateway-image` override available)
  and validate the rendered config's structure before mounting it; re-verify
  against the pinned version when bumping the digest.
- **Two VMs increase memory/startup overhead per session** → this change
  does not add a shared-gateway mode; a single project running several
  concurrent agent sessions gets one gateway per session. Acceptable for the
  initial scope; a shared-gateway mode is a candidate follow-on if memory
  pressure becomes a real complaint.
- **Gateway crash mid-session** → the single-destination firewall means the
  agent VM cannot fall back to direct provider access (fails closed), but
  the agent session itself will simply start failing requests until the user
  restarts it. No auto-restart pairing is built in this change.
- **Copilot CLI and subscription-passthrough users get no gateway coverage**
  → explicit warnings (Copilot) and outright rejection (passthrough modes)
  rather than a silent, false sense of coverage.
- **`container inspect` JSON shape is a third-party CLI contract** → the same
  risk already exists nowhere else in this repo (no prior Apple-`container`
  network inspection code); mitigate with a narrow parsing helper and a
  regression test pinned to the documented `.[0].networks[0].ipv4Address`
  shape, matching the verified syntax from the research.

## Migration Plan

Fully opt-in and additive: omitting `--gateway` leaves every existing code
path unchanged. No data migration. Rollback is deleting
`.project-sandbox/gateway/` and/or not passing `--gateway`. No changes to
existing flags' defaults or behavior.

## Open Questions

- Exact default gateway listen port (`3000` as in agentgateway's own
  examples, vs. `4000/4001` as used in the Claude-Code-through-agentgateway
  walkthrough) — does not affect the spec or task breakdown, pick one at
  implementation time and make it overridable.
- Whether the sentinel token should be a fixed per-session random value or a
  static placeholder string — either satisfies the "no real credential in the
  agent VM" requirement; a random per-session value is a modest additional
  precaution against a stale value being mistaken for a working credential
  outside the sandbox, and can be decided during implementation.
