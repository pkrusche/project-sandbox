## Why

Every current way to get a coding agent's LLM credentials into a
`project-sandbox` VM — forwarded OAuth credentials, staged API keys via
`--api-key-env` / `--api-key-env-file` — places a live, usable credential
inside the agent's filesystem or environment for the life of the session. A
compromised or over-permissioned agent process (malicious npm postinstall
hook, prompt injection, `/proc/self/environ` read) can exfiltrate it.

A local AI proxy such as `agentgateway` (Apache-2.0) can hold the real
provider credential on the host and expose an authenticated-upstream endpoint
the agent talks to instead. Rather than orchestrating such a proxy as a
managed sidecar (a second VM, per-project networks, health-gated lifecycles,
secrets partitioning), this change takes the simple path: the user configures
and starts the proxy locally themselves, following our documentation, and
`project-sandbox` provides CLI switches that point the built-in agents at it.
The agent VM then holds only a non-secret sentinel token that is worthless
off the host.

Scope is deliberately limited to the two agents whose provider configuration
is fully host-renderable as a custom-provider block: **pi** and **OpenCode**.
Claude Code and Codex CLI are not routed through the proxy and keep the
existing pass-through credential mechanism unchanged.

## What Changes

- Add `docs/agent-proxy.md` documenting how to install, configure, and start
  a local agent proxy (with a worked `agentgateway` example: loopback-bound
  listener, per-provider routes, credentials referenced from the proxy's own
  environment — never from the sandbox).
- Add an opt-in `--agent-proxy URL` flag that accepts a loopback proxy URL
  and is valid only with `--agent pi` or `--agent opencode`; any other agent
  selection rejects the flag with an error naming the supported agents.
- Add a repeatable `--agent-proxy-model ID` flag naming the model(s) the
  proxy exposes; these are baked into the generated provider configuration
  (first one is the default model where the agent needs one).
- **Credential exclusion:** `--agent-proxy` forces
  `--no-forward-credentials` behavior — no host agent credential is staged,
  mounted, or forwarded — and rejects `--api-key-env` /
  `--api-key-env-file` as conflicting. The agent receives only a sentinel
  API-key value.
- Bake proxy provider configuration per agent:
  - **pi**: generated `models.json` custom provider (proxy base URL,
    configured models, sentinel `apiKey`) plus `settings.json`
    `defaultProvider`/`defaultModel`, mirroring the existing `--pi-ollama`
    config shape.
  - **OpenCode**: generated `opencode.json` custom provider with a `baseURL`
    pointing at the proxy, the configured models, and a sentinel key.
- Reach the loopback-bound proxy from the agent VM by reusing the
  runtime-specific loopback-forwarding mechanism built for
  `local-ollama-support` (`ollama_network.py`'s strategy selection),
  generalized to the proxy's port, with a preflight TCP reachability check
  that fails the run before any container starts.
- Extend `firewall.py` / `init-firewall.sh.j2` with a port-scoped allow rule
  for the forwarded proxy endpoint (same pattern as the existing
  `--pi-ollama` rule); the rest of the firewall behavior is unchanged.
- `--dry-run --agent-proxy ...` prints the baked provider configuration and
  planned commands without writing files, starting containers, or requiring
  the proxy to be running.

**Explicitly not built (simplified away from the earlier sidecar design):**
proxy process orchestration (start/stop/health-gated lifecycle), per-project
container networks, gateway config generation and validation, host secrets
partitioning (`secrets.env`), egress collapse to a single destination, the
macOS-26 inter-container-networking gate, and Claude Code / Codex / Copilot
routing. Claude Code and Codex keep pass-through credentials; the proxy is
the user's process, managed by the user.

## Capabilities

### New Capabilities
- `agent-proxy-support`: opt-in routing of pi and OpenCode LLM traffic
  through a user-managed local proxy — CLI surface (`--agent-proxy`,
  `--agent-proxy-model`), credential exclusion, loopback reachability and
  firewall scoping, baked per-agent provider configuration, and local-proxy
  setup documentation.

### Modified Capabilities
- (none — `local-ollama-support`, `pi-agent-support`, and
  `dockerfile-splicing` requirements are untouched; the forwarding mechanism
  reuse is shared implementation, not a spec-level change to
  `local-ollama-support`)

## Impact

- **Code:** changes to `cli.py` (new flags, validation, credential
  exclusion), `config_agents.py` (pi/OpenCode proxy provider baking),
  `firewall.py` / `init-firewall.sh.j2` (proxy endpoint allow rule), and a
  generalization of `ollama_network.py`'s forwarding-strategy selection to a
  configurable port (shared helper or parametrized module). No new runtime
  processes and no container images pulled by `project-sandbox`.
- **Dependencies:** none. The proxy binary/image is installed and run by the
  user, outside `project-sandbox`.
- **Docs:** new `docs/agent-proxy.md` (setup + usage); `docs/security.md`
  gains a section on the credential-isolation posture this enables and its
  boundaries (loopback exposure, user-managed trust in the proxy).
- **Security-impacting:** yes — firewall changes and the credential-exclusion
  invariant are security-sensitive per `AGENTS.md`; the docs must state
  plainly that `project-sandbox` does not verify the proxy's own
  configuration or credential handling.
- **Platform constraint:** none beyond `local-ollama-support`'s existing
  reachability matrix — every runtime with a verified loopback-forwarding
  strategy works; no macOS-26 requirement.
