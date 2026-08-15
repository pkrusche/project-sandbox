## Why

Every current way to give a coding agent access to an LLM from a
`project-sandbox` VM also gives that VM a reusable provider credential:
forwarded OAuth state or API keys staged with `--api-key-env` /
`--api-key-env-file`. A compromised agent process, dependency hook, or
prompt-injected command can copy that credential and use it anywhere until it
expires or is revoked.

We will instead assume the user has configured and started the authenticated
local `agentgateway` service described by
[`pkrusche/agentgateway-locally`](https://github.com/pkrusche/agentgateway-locally).
That service keeps the OpenAI and Anthropic keys in its own container, exposes
an OpenAI-compatible LLM data plane on host loopback port 4000, and requires a
separate gateway bearer key on every request. `project-sandbox` only needs to
make that existing endpoint reachable and configure supported agents to use
it.

The gateway key is intentionally allowed inside the agent VM. It can authorize
spend through the running local gateway, so it is a real secret and must be
redacted, but it is not a provider credential and is not useful against OpenAI
or Anthropic after exfiltration. This change therefore provides **provider
credential isolation**, not protection from misuse of the local gateway during
a session.

Scope remains limited to **pi** and **OpenCode**, whose custom provider
configuration can be rendered completely by the host. Claude Code and Codex
CLI continue to use their existing credential paths.

## What Changes

- Add `docs/agent-proxy.md` treating `agentgateway-locally` as a prerequisite,
  linking to its setup and security documentation, and showing the supported
  `project-sandbox` invocation. The documentation does not duplicate or fork
  the gateway configuration.
- Add an opt-in `--agent-proxy URL` flag, accepted only with `--agent pi` or
  `--agent opencode`. For the documented setup the URL is
  `http://127.0.0.1:4000/v1`; port 3000 is MCP and is not an LLM endpoint.
- Add `--agent-proxy-key-env NAME`, naming a host environment variable that
  contains the gateway bearer key and defaulting to `AGENTGATEWAY_API_KEY`. If
  that variable is empty, read `pass show agentgateway-api-key` as the fallback
  used by the referenced setup. The value is staged only for the selected
  agent's proxy provider and redacted from diagnostics and dry-run output.
- Reuse the regular `--model` flag for model selection. Proxy mode requires it:
  pi uses `--model <model-id>`, while OpenCode uses
  `--model agent-proxy/<model-id>` to select the generated provider.
- **Provider credential exclusion:** proxy mode forces
  `--no-forward-credentials` behavior and rejects `--api-key-env` /
  `--api-key-env-file`. No host agent OAuth state or OpenAI/Anthropic key is
  staged, mounted, or forwarded. The dedicated gateway key is the only LLM
  credential admitted by this mode.
- Perform an authenticated, bounded `GET /v1/models` preflight before starting
  a sandbox. This proves the LLM listener is up, the gateway key is accepted,
  discovers the complete model catalog, and verifies the `--model` selection.
  Errors distinguish unavailable proxy, rejected key, malformed/empty catalog,
  and an unavailable selected model.
- Bake proxy provider configuration per supported agent:
  - **pi:** generated `models.json` with the forwarded proxy base URL, every
    discovered model, and gateway key, plus `settings.json` selecting the proxy
    provider. The regular `--model` argument selects the model for the run.
  - **OpenCode:** generated `opencode.json` custom provider with the forwarded
    proxy base URL, every discovered model, and gateway key. The regular
    `--model agent-proxy/<model-id>` argument selects the model for the run.
- Reach the loopback listener from the agent VM by generalizing the forwarding
  strategy used by `--pi-ollama` to a configurable port and internal hostname.
  Add the corresponding port-scoped firewall rule.
- Add a user-executable, stdlib-only end-to-end checker. It verifies the proxy
  with authenticated `/v1/models`, then runs one real headless pi session and
  one real headless OpenCode session through it and reports a clear pass/fail
  result for each.
- `--dry-run --agent-proxy ... --model ...` previews the redacted plan and marks
  model discovery/provider generation as deferred without reading the key,
  contacting the proxy, writing files, or starting containers.

`project-sandbox` does **not** install, configure, start, restart, stop, or
upgrade agentgateway. It does not manage a second VM, per-project networks,
gateway configuration, provider secrets, the admin UI, request logs, or MCP.

## Capabilities

### New Capabilities

- `agent-proxy-support`: opt-in routing of pi and OpenCode LLM traffic through
  the authenticated, user-managed `agentgateway-locally` service, including
  provider credential exclusion, gateway-key handling, authenticated
  preflight/model validation, loopback forwarding, firewall scoping, generated
  agent provider configuration, documentation, and an executable end-to-end
  check.

### Modified Capabilities

- (none — the local Ollama, pi agent, and credential-forwarding capabilities
  remain unchanged when proxy mode is absent)

## Impact

- **Code:** `cli.py` gains proxy flags, validation, gateway-key handling, and
  authenticated preflight; `config_agents.py` renders pi/OpenCode proxy
  providers; the Ollama forwarding helper is generalized; `firewall.py` and
  `init-firewall.sh.j2` gain a proxy endpoint rule. A stdlib-only script under
  `scripts/` performs the user-invoked end-to-end check.
- **Dependencies:** no Python package dependency. The external
  `agentgateway-locally` checkout, its prerequisites, provider keys, gateway
  key, and running gateway container are user-managed prerequisites.
- **Docs:** new `docs/agent-proxy.md`; `docs/security.md` explains the exact
  boundary. The external gateway's SQLite request log may contain full prompts
  and completions, and its documented cleanup/retention behavior remains the
  user's responsibility.
- **Security-impacting:** yes. Provider keys and OAuth credentials must never
  enter the agent VM; the gateway key must never be printed or included in a
  process argument; the gateway must retain strict listener authentication.
  Loopback publishing alone is not treated as a security boundary, including
  under Apple `container` vmnet networking.
- **Platform constraint:** no new macOS-26 requirement. The feature uses the
  existing local-Ollama forwarding strategy matrix and fails closed when no
  verified route to host loopback exists.
