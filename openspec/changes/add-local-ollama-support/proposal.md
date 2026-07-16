## Why

Pi (pi.dev's coding agent, added in `add-pi-agent-support`) is BYOK and today can only reach whatever providers are firewall-allowlisted. Users who run Ollama on the host cannot reach its default loopback listener from the sandbox because the container has its own network namespace. Requiring Ollama itself to bind to `0.0.0.0` makes its unauthenticated API available on every host interface; the sandbox instead needs a deliberately scoped bridge from a runtime-private host interface to Ollama's loopback listener.

## What Changes

- Add a single new CLI flag `--pi-ollama` (opt-in; no behavior change when absent) that, when passed with `--agent pi`:
  - Selects a runtime-specific, loopback-safe path to Ollama on `127.0.0.1:11434`,
  - Extends the container's firewall to reach only that endpoint on port 11434, and
  - Pre-configures Pi to use Ollama as its default provider.
- **Loopback preservation**: keep Ollama on its default `127.0.0.1:11434` listener. Never require or automatically configure `OLLAMA_HOST=0.0.0.0`.
- **Runtime networking adapter**: prefer each runtime's native loopback-forwarding mechanism (Apple `container` localhost DNS, Docker Desktop's host alias, and Podman's `host.containers.internal`/`pasta` path). For local Linux bridge runtimes without native loopback forwarding, use a managed `socat` proxy bound only to the bridge address. Never fall back to `0.0.0.0`; fail clearly when no safe path can be verified.
- **Firewall**: reuse and extend the existing gateway-discovery mechanism in `init-firewall.sh.j2` (today only wired for the devcontainer variant, via `allow_host_network`) so it also runs for direct CLI runs when `--pi-ollama` is set. Scope the new iptables rule to `tcp --dport 11434` only — narrower than every existing allow rule, which permits all ports to an IP — to avoid exposing arbitrary host-bound services.
- **Address handoff**: retain a fixed Pi-facing hostname (`ollama.project-sandbox.internal`) while the runtime adapter maps it to the verified native-forwarding or bridge-proxy endpoint. The firewall permits only the resolved endpoint on TCP port 11434. No `OLLAMA_HOST` env var or runtime JSON patching is needed.
- **Config baking**: extend `config_agents.py` with a new bake path for Pi (following the Claude/Codex "fresh JSON" `render()` pattern, not the OpenCode host-sync pattern — this reopens `add-pi-agent-support`'s explicit "no baked config for Pi" decision, scoped specifically to this flag) that writes:
  - `~/.pi/agent/models.json`: an `ollama` provider entry (`baseUrl: http://ollama.project-sandbox.internal:11434/v1`, `api: openai-completions`, `apiKey: ollama`, a model list).
  - `~/.pi/settings.json`: `defaultProvider: ollama`, `defaultModel: <first configured model>` (a new sibling path outside the currently-mounted `~/.pi/agent` tree, requiring new mount/provision plumbing).
- Add a `--ollama-model` CLI flag (repeatable, like the existing `--extra-domain`) to override the default model ID list baked into `models.json`.
- Update `container_cli.py` to add a `/project-sandbox-config/pi` mount (none exists today) and `_provision.sh.j2` to copy the two baked files to their two distinct target paths inside the container.
- Update docs (`docs/usage.md`, `docs/security.md`, `docs/runtime.md`) to describe the runtime matrix, any runtime-specific prerequisites, conditional `socat` use, lifecycle, and residual private-network exposure.
- Explicitly out of scope: generalizing this same local-provider pattern to OpenCode (a reasonable future follow-up, deferred); any `OLLAMA_HOST` env var; any transparent DNAT/redirect of literal `localhost:11434` traffic.

## Capabilities

### New Capabilities
- `local-ollama-support`: end-to-end support for reaching a host-run Ollama server from inside the sandboxed container when Pi is selected — scoped firewall access to the host gateway, fixed-hostname DNS pinning, and baked Pi provider/model configuration.

### Modified Capabilities
(none — this is additive; it does not change any existing requirement in `pi-agent-support`, it adds a new opt-in capability alongside it)

## Impact

- **Code**: `cli.py` (new flags and forwarding lifecycle), `container_cli.py` plus a focused networking adapter (native endpoint selection and conditional managed `socat`), `firewall.py` + `templates/init-firewall.sh.j2` (port-scoped endpoint allow and fixed-hostname pin), `config_agents.py`, and mount/provision plumbing.
- **Tests**: `tests/test_cli.py`, `tests/test_renderers.py` (firewall script content assertions), `tests/test_config_agents.py`, `tests/test_container_cli.py` — mirror existing coverage patterns for flag parsing, mount construction, and rendered config content.
- **Docs**: `docs/usage.md`, `docs/security.md`, `docs/runtime.md`.
- **Security-sensitive**: native forwarding configuration or a bridge proxy exposes Ollama to the selected runtime network; endpoint verification, owned-resource cleanup, and the port-scoped container firewall are security boundaries.
- **Dependencies**: `socat` is a host-side prerequisite only for runtime modes that require the Linux bridge fallback; no new npm or Python package is required.
