## Context

`project-sandbox` currently forwards host agent credentials or injects selected
API-key environment variables. Both place a provider credential in the agent
VM. This change adds a provider-isolating path built around the concrete local
deployment in `pkrusche/agentgateway-locally` rather than an abstract,
unauthenticated proxy.

That deployment has these relevant properties:

- agentgateway runs outside `project-sandbox`, under Docker or Apple
  `container`, and is managed with its own `run.py`;
- the LLM data plane is `http://127.0.0.1:4000`, with OpenAI-compatible paths
  below `/v1`;
- OpenAI and Anthropic provider keys come from `pass` and exist in the gateway
  container environment, never in its tracked configuration;
- every LLM request requires `Authorization: Bearer <gateway key>` under a
  strict API-key policy; and
- request logging is enabled and can persist full prompts and completions in
  the gateway's SQLite database.

The gateway key is a narrower credential, not a sentinel. A process in the
agent VM can use it to spend through the reachable local gateway. It cannot use
the key directly with OpenAI or Anthropic, and an off-host attacker cannot use
it unless the gateway is separately made reachable. The design protects
provider credentials from extraction and reuse; it does not prevent an active
sandbox from making authorized requests.

Existing `ollama_network.py` provides the runtime-specific mechanism for
reaching a host-loopback service without widening that service to `0.0.0.0`.
The implementation will generalize that mechanism to a supplied port and
hostname while preserving `--pi-ollama` behavior.

## Goals / Non-Goals

**Goals:**

- Keep OpenAI/Anthropic API keys and host agent OAuth state out of the agent VM
  whenever proxy mode is active.
- Admit only the gateway bearer key needed by the proxy's mandatory client
  authentication, and handle it as a secret in staging, diagnostics, dry-run,
  and process execution.
- Discover the complete gateway model catalog and validate the regular
  `--model` selection before any sandbox starts.
- Configure pi and OpenCode to use the authenticated OpenAI-compatible endpoint.
- Reuse the verified local-Ollama forwarding and firewall patterns.
- Give users one executable end-to-end check that exercises both supported
  agents through the real proxy.

**Non-Goals:**

- Installing or managing `agentgateway-locally`, its `pass` entries, provider
  keys, container, image pin, configuration, UI, logs, or lifecycle.
- Supporting an unauthenticated listener or a sentinel client key. The
  referenced setup deliberately requires client authentication.
- Routing Claude Code, Codex CLI, bash, MCP, or arbitrary application traffic.
- Guaranteeing that a compromised active sandbox cannot spend through the
  gateway or disclose prompts. The gateway key authorizes requests, and the
  referenced gateway stores request logs by default.
- Collapsing all agent egress to the proxy. Existing firewall behavior remains,
  with one additive proxy rule.
- Reproducing the external repository's configuration in this repository.

## Decisions

### Treat `agentgateway-locally` as a prerequisite

The project documentation links to the external setup and records only the
integration contract: authenticated LLM endpoint, gateway-key acquisition,
model aliases, and lifecycle ownership. This avoids maintaining a second copy
of security-sensitive agentgateway YAML or container configuration.

### Keep the existing pi/OpenCode scope

Both agents accept a host-rendered custom provider with an arbitrary OpenAI
base URL, model list, and API key. Claude Code and Codex require different
environment/configuration integration and remain unchanged. Proxy flags with
any unsupported agent fail before credential staging or container work.

### Accept the gateway key through a dedicated host environment variable

`--agent-proxy-key-env NAME` names the environment variable and defaults to
`AGENTGATEWAY_API_KEY`; it does not accept the secret value on the command line.
If the selected variable is empty, the CLI captures
`pass show agentgateway-api-key` as a fallback. Documentation can populate the
variable from `<agentgateway-locally>/run.py key`, consistent with that
repository's SDK usage. A dedicated option keeps the gateway key
distinguishable from forbidden provider-key injection.

The implementation reads the value only for a real run, never prints it, and
stages it only in the selected agent's private generated provider
configuration (or a provider-specific environment reference if the client
supports one without broadening exposure). File permissions and cleanup match
the existing private credential staging path. Failure of both the environment
and `pass` sources aborts before container work. Dry-run validates the variable
name but neither reads nor requires a key value.

### Force provider credential exclusion

Proxy mode behaves as `--no-forward-credentials`, purges stale staged agent
credentials, and rejects `--api-key-env` and `--api-key-env-file`. Tests inspect
the complete environment and mount plan to ensure no provider credential or
OAuth file is admitted. The gateway key is the sole explicit exception.

### Use an authenticated HTTP preflight

Before creating forwarding resources or starting a container, the CLI requests
`<proxy-base>/models` with `Authorization: Bearer <gateway key>` and a bounded
timeout. The response is the sole source of the proxy provider's model catalog:
every non-empty string in the OpenAI-compatible `data[].id` fields is preserved
exactly, duplicate IDs are removed while preserving response order, and the
result is rendered into the selected agent's provider configuration. An empty
catalog is an error. This replaces a weak TCP-connect probe and gives actionable
failures:

- connection/timeout: start or troubleshoot `agentgateway-locally`;
- 401/403: refresh or correct the gateway key;
- malformed or empty response: endpoint is not the expected LLM API; and
- absent selected model: correct `--model` or the external gateway
  configuration.

Proxy mode removes the separate `--agent-proxy-model` surface and requires the
existing `--model` argument. Pi receives the discovered model ID unchanged
(`--model <model-id>`). OpenCode's existing syntax includes the generated
provider ID (`--model agent-proxy/<model-id>`); validation strips that prefix
before comparing with discovery and rejects any other provider prefix. This
keeps one model-selection mechanism across ordinary and proxy-backed runs.

Dry-run skips the request, reports discovery/provider generation as deferred,
and validates only the shape of `--model` because no catalog is available. The
preflight does not start, restart, or mutate the gateway.

### Keep a loopback URL and reuse runtime forwarding

`--agent-proxy` accepts an HTTP loopback URL with an explicit port and optional
`/v1` suffix. The documented value is `http://127.0.0.1:4000/v1`. It rejects
wildcard and non-loopback hosts. The generalized forwarding helper rewrites the
host portion to a dedicated in-container hostname while preserving scheme,
port, and path. Runtime selection and safe fallback behavior match local Ollama.

The firewall adds a TCP allow rule scoped to that forwarded hostname/address
and port. This does not assert that loopback is a security boundary: the strict
gateway API-key policy remains required, especially because Apple `container`
ports may also be reachable over vmnet.

### Render agent-specific providers

For pi, generated `models.json` contains an OpenAI-completions provider with the
forwarded `/v1` base URL, every model returned by discovery, and the gateway
key; `settings.json` selects the proxy provider but does not choose a default
model. For OpenCode, generated `opencode.json` contains the equivalent custom
provider and every discovered model. In both cases the existing `--model`
dispatch selects the model for the run. Secret-bearing files use private
staging, are mounted only for the selected agent, and are never shown verbatim.

### Ship a user-executable end-to-end checker

Add `scripts/check-agent-proxy.py`, stdlib-only. The script accepts the path to
an `agentgateway-locally` checkout and pi/OpenCode model selections. It:

1. verifies prerequisites without changing gateway state;
2. obtains the gateway key by invoking `pass show agentgateway-api-key` with captured output (we don't rely on having access to run.py - if the pass entry is different users can adjust the script);
3. performs the same authenticated `/v1/models` health/auth/discovery check and
   validates both selections;
4. creates an isolated temporary project;
5. runs a headless pi session through the proxy using regular
   `--model <model-id>` and a prompt requiring a unique exact marker;
6. runs the equivalent headless OpenCode session using regular
   `--model agent-proxy/<model-id>` and a different marker; and
7. reports per-agent pass/fail and exits nonzero if either command fails or its
   marker is absent.

The key is passed to `project-sandbox` through the dedicated environment
variable, never argv, and is redacted from command/error output. The script
does not call `run.py up`, change gateway config, or retain its temporary
project. Unit tests mock HTTP and subprocess boundaries; the real two-agent run
is intentionally user-invoked because it consumes provider tokens.

## Risks / Trade-offs

- **Gateway key theft permits local spend** → state the boundary plainly;
  minimize staging, redact it, and rely on the external setup's strict auth and
  easy key rotation. This is narrower than provider-key theft but not harmless.
- **External configuration can drift** → authenticated `/v1/models` verifies
  the integration contract at runtime; docs link to the known setup rather than
  copying its YAML.
- **Prompt/completion persistence** → documentation points to the external
  request-log retention and pruning guidance before users run sensitive work.
- **Loopback is not confinement under every runtime** → require auth regardless
  of bind address and never recommend an unauthenticated listener.
- **Generated config contains the gateway key** → keep it under the private
  temporary credential root, mount only for the chosen agent, clean it with the
  existing credential lifecycle, and exclude it from dry-run/transcripts.
- **E2E checks cost tokens** → run only on explicit user invocation, use minimal
  deterministic prompts, and announce that the check makes two paid requests.

## Migration Plan

The feature is additive. Without `--agent-proxy`, behavior is unchanged. Users
first configure and start `agentgateway-locally`, export its gateway key into a
chosen host variable, then opt into proxy mode. Rollback is omitting the proxy
flags. No prior sidecar or sentinel implementation shipped, so no stored state
requires migration.

## Decision notes

- `--agent-proxy-key-env` defaults to `AGENTGATEWAY_API_KEY`; if that is empty,
  try reading via `pass show agentgateway-api-key`; if that fails, print an error.
- The E2E checker defaults to discovered alias `gpt-5-mini` for both agents,
  parameterizable via `AGENT_PROXY_TEST_MODEL`, and passes that selection
  through each invocation's regular `--model` option.
