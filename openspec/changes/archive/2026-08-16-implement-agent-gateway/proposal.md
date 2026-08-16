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

Scope includes **pi**, **OpenCode**, and **Bash**. Pi and OpenCode receive custom
provider configuration; interactive and headless Bash receive the equivalent
standard OpenAI-compatible environment. Claude Code and Codex CLI continue to
use their existing credential paths.

## What Changes

- Add `docs/agent-proxy.md` treating `agentgateway-locally` as a prerequisite,
  linking to its setup and security documentation, and showing the supported
  `project-sandbox` invocation. The documentation does not duplicate or fork
  the gateway configuration.
- Add an opt-in `--agent-proxy URL` flag, accepted with `--agent pi`,
  `--agent opencode`, or `--agent bash`. For the documented setup the URL is
  `http://127.0.0.1:4000/v1`; port 3000 is MCP and is not an LLM endpoint.
- Resolve the gateway bearer key without access to the external checkout:
  first capture `pass show agentgateway-api-key`, then fall back to the host
  environment variable named by `--agent-proxy-key-env NAME` (default
  `AGENTGATEWAY_API_KEY`), then to an explicit `--agent-proxy-key KEY`. The raw
  CLI form is the least-safe fallback because shells and process listings may
  expose argv. Regardless of source, the value is staged only for the selected
  agent's proxy provider and redacted from project-sandbox output.
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
  - **Bash:** standard `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`
    variables for both interactive shells and headless prompt commands, plus
    pre-configured Pi and OpenCode providers using the selected default model.
- Reach the loopback listener from the agent VM by generalizing the forwarding
  strategy used by `--pi-ollama` to a configurable port and internal hostname.
  Add the corresponding port-scoped firewall rule. In proxy mode, omit normal
  provider domains and the broad devcontainer host-gateway exception; retain
  only user-requested `--extra-domain` / `--allow-github` additions.
- Add a user-executable, stdlib-only end-to-end checker. It verifies the proxy
  with authenticated `/v1/models`, then runs one real headless pi session and
  one real headless OpenCode session through it and reports a clear pass/fail
  result for each.
- `--dry-run --agent-proxy ... --model ...` previews the redacted plan and marks
  model discovery/provider generation as deferred without invoking pass,
  reading an environment key, contacting the proxy, writing files, or starting
  containers. A raw CLI fallback is parsed but never echoed.

`project-sandbox` does **not** install, configure, start, restart, stop, or
upgrade agentgateway. It does not manage a second VM, per-project networks,
gateway configuration, provider secrets, the admin UI, request logs, or MCP.

## Capabilities

### New Capabilities

- `agent-proxy-support`: opt-in routing of pi, OpenCode, and Bash LLM traffic through
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
- **Dependencies:** no Python package dependency and no runtime access to an
  `agentgateway-locally` checkout. The configured service, provider keys,
  gateway key in `pass` (or an explicit fallback), and running gateway
  container are user-managed prerequisites.
- **Docs:** new `docs/agent-proxy.md`; `docs/security.md` explains the exact
  boundary. The external gateway's SQLite request log may contain full prompts
  and completions, and its documented cleanup/retention behavior remains the
  user's responsibility.
- **Security-impacting:** yes. Provider keys and OAuth credentials must never
  enter the agent VM; the gateway key must never be printed. Pass and
  environment lookup keep it out of argv; the explicit CLI fallback cannot and
  is documented with a warning. The gateway must retain strict listener
  authentication. Loopback publishing alone is not treated as a security
  boundary, including under Apple `container` vmnet networking.
- **Platform constraint:** no new macOS-26 requirement. The feature uses the
  existing local-Ollama forwarding strategy matrix and fails closed when no
  verified route to host loopback exists.
