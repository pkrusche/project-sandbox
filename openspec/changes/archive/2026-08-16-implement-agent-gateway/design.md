## Context

`project-sandbox` currently forwards host agent credentials or injects selected
API-key environment variables. Both place a provider credential in the agent
VM. This change adds a provider-isolating path built around the concrete local
deployment in `pkrusche/agentgateway-locally` rather than an abstract,
unauthenticated proxy.

That deployment has these relevant properties:

- agentgateway runs outside `project-sandbox`, under Docker or Apple
  `container`, and its lifecycle is managed independently; project-sandbox and
  its verification script cannot invoke the external checkout's `run.py`;
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
- Configure pi, OpenCode, and interactive/headless Bash to use the authenticated
  OpenAI-compatible endpoint.
- Reuse the verified local-Ollama forwarding and firewall patterns.
- Give users one executable end-to-end check that exercises both supported
  agents through the real proxy.

**Non-Goals:**

- Installing or managing `agentgateway-locally`, its `pass` entries, provider
  keys, container, image pin, configuration, UI, logs, or lifecycle.
- Supporting an unauthenticated listener or a sentinel client key. The
  referenced setup deliberately requires client authentication.
- Routing Claude Code, Codex CLI, MCP, or arbitrary application traffic.
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

### Support pi, OpenCode, and Bash explicitly

Pi and OpenCode accept a host-rendered custom provider with an arbitrary OpenAI
base URL, model list, and API key. Bash receives the same connection through
the standard `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` environment
contract, which naturally covers both interactive shells and headless prompt
commands. Claude Code and Codex require different configuration integration and
remain unchanged. Proxy flags with any unsupported agent fail before credential
staging or container work.

### Resolve the gateway key from pass with explicit fallbacks

The CLI does not depend on the external checkout. On a real run it resolves one
gateway key in this order:

1. capture `pass show agentgateway-api-key`;
2. if unavailable or empty, read the environment variable named by
   `--agent-proxy-key-env NAME`, defaulting to `AGENTGATEWAY_API_KEY`; and
3. if still unavailable, use `--agent-proxy-key KEY`.

The pass entry name is fixed to match the referenced setup. The environment
name is configurable for users with a different export convention. The raw
key option is an explicit last resort: it is redacted by project-sandbox after
argument parsing, but the program cannot prevent the invoking shell, history,
or local process inspection from seeing argv. Help and security documentation
warn about this and recommend pass or environment lookup.

The implementation reads secret sources only for a real run, never prints the
resolved value, and stages it only in the selected agent's private generated
provider configuration (or a provider-specific environment reference if the
client supports one without broadening exposure). File permissions and cleanup
match the existing private credential staging path. Failure of all three
sources aborts before network or container work. Dry-run validates option shape
but does not invoke pass or read environment values; a supplied CLI value is
already present in argv and is immediately replaced with a redaction marker in
all generated diagnostics.

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
port, and path. On Apple `container`, the runtime-specific hostname is
`host.docker.internal`, which the user configures with the runtime's localhost
DNS command. The CLI never invokes `sudo`; it prints the exact command and
warns that the DNS change might disable network connectivity and should be
followed by a container-system restart. Runtime selection and safe fallback
behavior otherwise match local Ollama.

The firewall adds a TCP allow rule scoped to that forwarded hostname/address
and port, then probes the endpoint after the final default-deny rules are in
place. Proxy mode omits the normal OpenAI/Anthropic domains and the
devcontainer's broad host-gateway exception. `--extra-domain` and
`--allow-github` remain explicit opt-ins; prompt inspection never widens proxy
mode automatically. This does not assert that loopback is a security boundary: the strict
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
Bash instead receives the forwarded base URL, gateway key, and selected model
through standard OpenAI environment variables. Docker and Podman inherit bare
variable names from the protected subprocess environment; Apple `container`
uses the existing mode-0600 staged env-file path, which proxy cleanup removes.
Bash can also launch Pi or OpenCode, so its session renders and mounts both
private provider configs. Pi's `defaultProvider` / `defaultModel` and
OpenCode's top-level `model` select the requested model without extra shell
arguments; the configs share the discovered catalog and gateway key and follow
the same post-session cleanup.

### Ship a user-executable end-to-end checker

Add `scripts/check-agent-proxy.py`, stdlib-only. The script accepts the proxy URL
and pi/OpenCode model selections, with no external-checkout path. It:

1. verifies local project-sandbox/runtime prerequisites without changing
   gateway state;
2. resolves the key from `pass show agentgateway-api-key`, then its environment
   fallback, then an explicit script CLI-key fallback;
3. performs the same authenticated `/v1/models` health/auth/discovery check and
   validates both selections;
4. creates an isolated temporary project;
5. runs a headless pi session through the proxy using regular
   `--model <model-id>` and a prompt requiring a unique exact marker;
6. runs the equivalent headless OpenCode session using regular
   `--model agent-proxy/<model-id>` and a different marker; and
7. reports per-agent pass/fail and exits nonzero if either command fails or its
   marker is absent.

The checker passes the resolved key to each child `project-sandbox` process
through the dedicated environment variable rather than repeating it in child
argv, and redacts it from command/error output. If the user supplied the key to
the checker as a raw CLI option, its original argv exposure cannot be undone
and receives the same warning. The script does not start, stop, or reconfigure
the gateway and does not retain its temporary project. Unit tests mock HTTP and
subprocess boundaries; the real two-agent run is intentionally user-invoked
because it consumes provider tokens.

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
- **Raw CLI fallback exposes the gateway key in argv** → keep it last in the
  lookup order, warn in help/docs and at use, redact subsequent output, and
  recommend pass or environment lookup instead.
- **E2E checks cost tokens** → run only on explicit user invocation, use minimal
  deterministic prompts, and announce that the check makes two paid requests.

## Migration Plan

The feature is additive. Without `--agent-proxy`, behavior is unchanged. Users
first configure and start `agentgateway-locally`, export its gateway key into a
chosen host variable, then opt into proxy mode. Rollback is omitting the proxy
flags. No prior sidecar or sentinel implementation shipped, so no stored state
requires migration.

## Decision notes

- Gateway key precedence is `pass show agentgateway-api-key`, then the selected
  environment variable (default `AGENTGATEWAY_API_KEY`), then the raw
  `--agent-proxy-key` fallback; failure of all three is an error.
- The E2E checker defaults to discovered alias `gpt-5-mini` for both agents,
  parameterizable via `AGENT_PROXY_TEST_MODEL`, and passes that selection
  through each invocation's regular `--model` option.
