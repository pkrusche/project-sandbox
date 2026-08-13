## Why

Every current way to get a coding agent's LLM credentials into a `project-sandbox`
VM — forwarded OAuth credentials, staged API keys via `--api-key-env` /
`--api-key-env-file` — places a live, usable credential inside the agent's
filesystem or environment for the life of the session. A compromised or
over-permissioned agent process (malicious npm postinstall hook, prompt
injection, `/proc/self/environ` read) can exfiltrate it. `agentgateway`, an
Apache-2.0 AI-native proxy, can hold the real provider credential outside the
agent VM and hand the agent only a per-session sentinel token that is worthless
off the sandbox network — closing this gap without changing how the agents are
invoked.

## What Changes

- Add an opt-in `--gateway[=auto|on|off]` flag that launches `agentgateway`
  (`ghcr.io/agentgateway/agentgateway`, pinned by digest) as a second Apple
  `container` VM on a per-project network, and points the agent VM's LLM
  traffic at it instead of the real provider.
- **BREAKING (additive, opt-in only):** `--gateway on` forces
  `--no-forward-credentials` and is non-overridable — passing both is not an
  error, but no credential is ever staged or mounted while the gateway is
  active. A preflight assertion fails the run if a known credential path would
  land in the agent home while `--gateway on` is set.
- Add a new jinja2 template (`agentgateway-config.yaml.j2`) that renders a
  validated agentgateway routing config (`binds → listeners → routes →
  backends: ai`) with one route per enabled provider, referencing secrets only
  as `$ENV_VAR` — never inlined.
- Partition a single host `secrets.env` (0600) into gateway-only secrets
  (mounted read-only into the gateway VM's env, never the agent VM) versus
  agent-visible config (base URLs, sentinel tokens).
- Extend `firewall.py` / `init-firewall.sh.j2` so that when the gateway is
  active the agent VM's egress allowlist collapses to a single destination
  (`GATEWAY_IP:<port>`, default DROP otherwise); the gateway VM keeps the
  existing curated provider-domain allowlist.
- Wire per-agent base-URL configuration for Claude Code
  (`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` sentinel), Codex CLI
  (custom `model_providers` entry in generated `config.toml`), and OpenCode
  (custom provider `baseURL`). GitHub Copilot CLI is explicitly unsupported
  for GitHub-hosted models (upstream limitation, tracked as a warning) and
  only reachable in BYOK mode.
- Orchestrate the sidecar's lifecycle in `container_cli.py`: per-project
  network create, gateway VM start, IP discovery via `container inspect`,
  a health-check gate before the agent VM starts, and teardown of both the
  gateway container and the network on exit. Hard-fail with a clear message
  on macOS < 26 (Apple `container` cannot do inter-container networking
  there).

**Deferred to follow-on changes (not built here):** macOS Keychain-backed
secrets assembly (`--gateway-keychain`), the Docker/Podman peer-container
path, devcontainer sidecar support (the repo's devcontainer generator does
not currently emit `docker-compose.yml` at all — adding a compose-based
multi-service devcontainer is its own scoped change), and publishing the
admin UI / Prometheus metrics port. These are listed in `design.md` for
context but have no requirements or tasks in this change.

## Capabilities

### New Capabilities
- `agentgateway-sidecar`: opt-in LLM credential-isolating proxy sidecar for
  Apple `container` sessions — CLI surface, config generation, secrets
  partitioning, sidecar lifecycle/orchestration, firewall collapse, and
  per-agent base-URL wiring for Claude Code, Codex CLI, and OpenCode.

### Modified Capabilities
- (none — no existing spec's requirements change; `local-ollama-support`,
  `pi-agent-support`, and `dockerfile-splicing` are unaffected because the
  gateway is a distinct opt-in path that only interacts with credential
  forwarding, which is CLI behavior, not a documented spec requirement of
  those capabilities)

## Impact

- **Code:** new `src/project_sandbox/agentgateway.py` (config rendering,
  secrets partitioning) and `src/project_sandbox/gateway_network.py`
  (Apple `container` network/lifecycle orchestration, mirroring
  `ollama_network.py`'s structure); changes to `cli.py` (new flags, wiring),
  `container_cli.py` (gateway-specific argv builders), `firewall.py` /
  `init-firewall.sh.j2` (single-destination collapse), `config_agents.py`
  (Codex/OpenCode provider config baking), new template
  `templates/agentgateway-config.yaml.j2`.
- **Dependencies:** none new at the Python level (jinja2 already required);
  pulls a new container image (`ghcr.io/agentgateway/agentgateway`) at
  runtime, pinned by digest.
- **Docs:** new `docs/gateway.md`; `docs/security.md` gets a section
  describing the credential-isolation invariant this feature provides.
- **Security-impacting:** yes — this is the mechanism `docs/security.md`
  will document for the strongest available credential-isolation posture;
  firewall and secrets-handling changes are security-sensitive per
  `AGENTS.md`.
- **Platform constraint:** requires macOS 26 for the Apple `container`
  runtime; unsupported on macOS 15 and on the `chroot` runtime.
