## Why

Pi (pi.dev's coding agent, added in `add-pi-agent-support`) is BYOK and today can only reach whatever providers are firewall-allowlisted (Anthropic/OpenAI by default, `--extra-domain` for anything else). Users who run local models via Ollama on the host have no way to reach them from inside the sandbox: the container runs in its own network namespace under every supported runtime (Apple `container`, Docker, Podman — none is passed `--network host`, and there is no `host.docker.internal`-equivalent alias anywhere in this codebase), so `localhost` inside the container is the container's own loopback, never the host's. Reaching a host-bound Ollama server needs deliberate host-gateway plumbing, not just a firewall allowlist entry, and Pi needs to be told about the provider before it can use it.

## What Changes

- Add a single new CLI flag `--pi-ollama` (opt-in; no behavior change when absent) that, when passed with `--agent pi`:
  - Extends the container's firewall to reach the host's Ollama server, and
  - Pre-configures Pi to use Ollama as its default provider.
- **Firewall**: reuse and extend the existing gateway-discovery mechanism in `init-firewall.sh.j2` (today only wired for the devcontainer variant, via `allow_host_network`) so it also runs for direct CLI runs when `--pi-ollama` is set. Scope the new iptables rule to `tcp --dport 11434` only — narrower than every existing allow rule, which permits all ports to an IP — to avoid exposing arbitrary host-bound services.
- **Address handoff**: pin a fixed, conventional hostname (`ollama.project-sandbox.internal`) to the discovered gateway IP in `/etc/hosts` at container startup, reusing the same `# project-sandbox-dns-pin`-style mechanism already used for regular allowlisted domains. No `OLLAMA_HOST` env var, no runtime JSON patching — because the hostname is fixed and known in advance, Pi's config can be baked host-side with the hostname literally in it.
- **Config baking**: extend `config_agents.py` with a new bake path for Pi (following the Claude/Codex "fresh JSON" `render()` pattern, not the OpenCode host-sync pattern — this reopens `add-pi-agent-support`'s explicit "no baked config for Pi" decision, scoped specifically to this flag) that writes:
  - `~/.pi/agent/models.json`: an `ollama` provider entry (`baseUrl: http://ollama.project-sandbox.internal:11434/v1`, `api: openai-completions`, `apiKey: ollama`, a model list).
  - `~/.pi/settings.json`: `defaultProvider: ollama`, `defaultModel: <first configured model>` (a new sibling path outside the currently-mounted `~/.pi/agent` tree, requiring new mount/provision plumbing).
- Add a `--ollama-model` CLI flag (repeatable, like the existing `--extra-domain`) to override/extend the default model ID list baked into `models.json`.
- Update `container_cli.py` to add a `/project-sandbox-config/pi` mount (none exists today) and `_provision.sh.j2` to copy the two baked files to their two distinct target paths inside the container.
- Update docs (`docs/usage.md`, `docs/security.md`, `docs/runtime.md`) to describe the new flag, its security scope, and Ollama host-side prerequisites (binding beyond loopback).
- Explicitly out of scope: generalizing this same local-provider pattern to OpenCode (a reasonable future follow-up, deferred); any `OLLAMA_HOST` env var; any transparent DNAT/redirect of literal `localhost:11434` traffic.

## Capabilities

### New Capabilities
- `local-ollama-support`: end-to-end support for reaching a host-run Ollama server from inside the sandboxed container when Pi is selected — scoped firewall access to the host gateway, fixed-hostname DNS pinning, and baked Pi provider/model configuration.

### Modified Capabilities
(none — this is additive; it does not change any existing requirement in `pi-agent-support`, it adds a new opt-in capability alongside it)

## Impact

- **Code**: `cli.py` (new `--pi-ollama` / `--ollama-model` flags), `firewall.py` + `templates/init-firewall.sh.j2` (port-scoped gateway allow for direct CLI runs, fixed-hostname `/etc/hosts` pin), `config_agents.py` (new Pi config-bake path), `container_cli.py` (new `/project-sandbox-config/pi` mount), `templates/_provision.sh.j2` (copy step for the two baked files to their distinct target paths).
- **Tests**: `tests/test_cli.py`, `tests/test_renderers.py` (firewall script content assertions), `tests/test_config_agents.py`, `tests/test_container_cli.py` — mirror existing coverage patterns for flag parsing, mount construction, and rendered config content.
- **Docs**: `docs/usage.md`, `docs/security.md`, `docs/runtime.md`.
- **Security-sensitive**: first extension of host-gateway network reachability to direct CLI runs (today devcontainer-only); new port-scoped firewall rule shape; new baked-config mount for an agent that previously had none. Falls under this repo's "treat generated container config and firewall behavior as security-sensitive" guidance.
- **Dependencies**: none new (no new npm/pip packages).
