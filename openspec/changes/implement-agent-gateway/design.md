## Context

`project-sandbox` currently has two ways an agent VM gets LLM credentials:
forwarded host OAuth credentials (`config_agents.sync_credentials`, disabled
by `--no-forward-credentials`) and staged API keys (`--api-key-env` /
`--api-key-env-file`, which require `--no-forward-credentials`). Both place a
live, usable credential inside the agent VM for the session's duration.

This change adds a third path: the user runs an AI proxy (e.g.
`agentgateway`) locally on the host, holding the real provider credentials in
its own environment, and `project-sandbox` points the agent at it. An earlier
draft of this change orchestrated the proxy as a managed sidecar VM
(per-project `container network`, health-gated startup, generated gateway
config, partitioned `secrets.env`, egress collapse, a hard macOS-26 gate).
That draft is superseded: the proxy is now entirely user-managed, which
deletes the whole orchestration surface and its platform constraint.

Relevant in-repo prior art:
- `ollama_network.py` already solves "reach a loopback-bound host service
  from the agent VM" per runtime (Apple `container` localhost DNS, native
  host-gateway mappings, a managed `socat` bridge fallback), with a
  port-scoped firewall rule and baked pi provider config
  (`--pi-ollama`). This change generalizes that mechanism to the proxy's
  port rather than inventing anything new.
- The credential-forwarding flag is `--no-forward-credentials` (`cli.py`);
  `--api-key-env` / `--api-key-env-file` already model "explicit credential
  injection" and give the conflict surface to reject.
- pi's custom-provider config (`models.json` `providers.<id>.baseUrl` /
  `apiKey` / `models`, `settings.json` `defaultProvider`) is already
  rendered by `config_agents.py` for the Ollama case; OpenCode supports the
  equivalent via a custom provider block in `opencode.json`.

## Goals / Non-Goals

**Goals:**
- No API key or OAuth credential is ever staged, mounted, or set as an
  environment variable inside the agent VM when `--agent-proxy` is active;
  the agent holds only a non-secret sentinel.
- Keep `project-sandbox`'s job small: validate flags, bake provider config,
  make the loopback proxy reachable, scope the firewall, fail fast if the
  proxy is not listening. Never start, stop, configure, or supervise the
  proxy itself.
- Reuse existing mechanisms: the `ollama_network.py` forwarding strategies,
  the `firewall.render(...)` port-scoped-rule pattern, and
  `config_agents.py`'s provider-config rendering.
- Support exactly the agents whose provider config is fully host-renderable
  as a custom provider: pi and OpenCode.

**Non-Goals (this change):**
- Proxy lifecycle orchestration of any kind — no sidecar VM, no
  `container network create`, no health-gated startup beyond a single
  preflight TCP check, no teardown. The proxy outlives and predates
  sessions at the user's discretion.
- Claude Code and Codex CLI routing. Both keep the existing pass-through
  credential mechanism unchanged; nothing in this change alters their
  behavior. (They can be revisited later; their exclusion here is a scoping
  decision, not a technical impossibility.)
- Generating or validating the proxy's own configuration. The docs give a
  worked `agentgateway` example; `project-sandbox` never reads or writes
  proxy config.
- Client authentication between agent and proxy. The baked key is a
  sentinel; proxies that require a real client key on the listener are not
  supported in this change (documented, and a candidate follow-on).
- Egress collapse (allowing *only* the proxy from the agent VM). The normal
  firewall allowlist stays as-is, plus one port-scoped proxy rule. With no
  credential in the VM, direct provider egress is unauthenticated; collapse
  is a hardening follow-on, not a prerequisite for the isolation goal.
- Verifying the proxy's upstream credential handling, TLS posture, or
  logging. That trust boundary belongs to the user and is documented as
  such.

## Decisions

**User-managed local proxy over orchestrated sidecar.** The superseding
decision. Rationale: the orchestration draft required a second VM per
session, per-project network management, IP discovery, health gating,
teardown paths, secrets partitioning, and a macOS-26 floor — all to stand up
a process the user can start with one documented command. A user-managed
proxy also naturally serves several concurrent sessions and survives session
restarts. The cost is that `project-sandbox` can no longer *guarantee* the
proxy's config is sane (e.g. that it doesn't log prompts or inline secrets);
the docs own that guidance instead.

**pi and OpenCode only.** Both accept a custom provider with an arbitrary
`baseUrl`/`baseURL`, a model list, and an API key from host-renderable
config files that `project-sandbox` already generates or stages. Claude Code
and Codex would need env-var/base-URL wiring interleaved with their
credential handling — precisely the surface this change avoids touching;
per the requesting decision they remain on pass-through credentials.

**Loopback-only proxy URL, reached via the existing forwarding
strategies.** `--agent-proxy` accepts only a loopback URL
(`http://127.0.0.1:<port>` shaped). The agent VM reaches it through the same
runtime-strategy selection `--pi-ollama` uses, generalized to the proxy's
port, under a dedicated hostname (e.g. `agent-proxy.project-sandbox.internal`
for the Apple localhost-DNS strategy). This keeps the proxy off `0.0.0.0`
and inherits an already-tested reachability matrix instead of adding a new
one. Runtimes with no safe strategy fail with the same
unsupported-mode-style error `local-ollama-support` defines.

**Sentinel API key, not a forwarded key.** The baked provider config carries
a fixed non-secret sentinel so the agent's client libraries see *an* API key
without a real one existing in the VM. Whether it is a static string or a
per-session random value is an implementation detail (see Open Questions).

**Preflight TCP check instead of a managed health gate.** Before any
container starts, the CLI attempts one bounded TCP connect to the proxy URL
and aborts with a clear "is your proxy running? see docs/agent-proxy.md"
error on failure. Skipped under `--dry-run`. This catches the dominant
failure mode (proxy not started) without any lifecycle coupling; a proxy
dying mid-session simply fails the agent's requests, which is acceptable and
documented.

**Additive firewall rule, not egress collapse.** Firewall rendering gains a
proxy endpoint parameter producing one port-scoped ACCEPT rule, exactly like
the `pi_ollama` rule; everything else is unchanged. Rationale in Non-Goals.
As with `--pi-ollama`, `--no-firewall` skips the hostname pin and rule, so
the CLI reuses the same style of warning that the baked hostname will not
resolve.

**Mutual exclusion with `--pi-ollama`.** Both features bake pi's
`models.json`/`defaultProvider`; combining them in one session is rejected
rather than merged. Merging is possible later if a real use case appears.

## Risks / Trade-offs

- **The proxy config is out of our control** → a misconfigured proxy (wrong
  upstream key, prompt logging, listener on `0.0.0.0`) silently weakens the
  posture. Mitigation: `docs/agent-proxy.md` gives a known-good
  `agentgateway` example (loopback bind, env-var credential references) and
  `docs/security.md` states the trust boundary explicitly.
- **Loopback proxy is host-global** → any local process can use the proxy
  and thus the credentials behind it. Same exposure class as a
  loopback-bound Ollama; documented, with client-auth support noted as a
  follow-on.
- **Sentinel breaks against proxies that validate client keys** → rejected
  scope for now; the docs say the listener must not require client
  authentication.
- **`agentgateway` is pre-1.0** and its config schema may shift → this now
  only affects the documented example, not code; the docs pin the version
  the example was verified against.
- **Model list is user-asserted** → `--agent-proxy-model` values are baked
  as-given; a typo surfaces as a provider/model error inside the agent, not
  at preflight. Acceptable; the CLI cannot enumerate an arbitrary proxy's
  models cheaply.

## Migration Plan

Fully opt-in and additive: omitting `--agent-proxy` leaves every existing
code path unchanged, including Claude Code / Codex credential pass-through
and `--pi-ollama`. No data migration; rollback is not passing the flag.
Relative to the superseded sidecar draft, nothing was ever shipped, so there
is no deprecation path to manage.

## Open Questions

- Sentinel value: static placeholder vs. per-session random string. Either
  satisfies "no real credential in the VM"; random slightly reduces the risk
  of the value being mistaken for a working key elsewhere. Decide at
  implementation time.
- pi provider `api` dialect for the baked config: default to
  `openai-completions` (matching the Ollama path and agentgateway's
  OpenAI-compatible routes) — whether an override flag is worth adding can
  be decided when the docs example is verified end-to-end.
- Whether `--agent-proxy-model` should have a default when omitted (probably
  not: fail with a clear "name at least one model" error, since we cannot
  guess what the proxy serves).
