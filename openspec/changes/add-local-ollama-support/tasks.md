## 1. Confirmed unknowns

- 1.1 Default Ollama model ID list for `models.json` — not yet decided; pick a small, reasonable default set and make it easy to override.
- 1.2 `--ollama-model` flag shape — assume repeatable single-value (`action="append"`), mirroring `--extra-domain`.
- 1.3 `settings.json`'s `defaultModel` — assume it is simply the first entry of the resolved model list (defaults or user-supplied), unless a dedicated `--ollama-default-model` flag proves necessary during implementation.
- 1.4 Ollama port — hardcode `11434` for v1; no `--ollama-port` flag.
- 1.5 `settings.json`'s `lastChangelogVersion` — check whether Pi treats a stale/mismatched value as meaningful (e.g. triggers a changelog prompt); if not, pin to Pi's current npm version pin (`0.80.6`) for realism, otherwise omit the field.

## 2. CLI flags

- [ ] 2.1 Add `--pi-ollama` flag to `cli.py` (`action="store_true"`, no-op unless `--agent pi` is also selected)
- [ ] 2.2 Add `--ollama-model` flag to `cli.py` (`action="append"`, default `[]`), documented as only meaningful with `--pi-ollama`
- [ ] 2.3 Validate/no-op gracefully when `--pi-ollama` or `--ollama-model` is passed without `--agent pi` (per spec: no Ollama-specific behavior triggered)

## 3. Firewall: port-scoped gateway access

- [ ] 3.1 In `firewall.py`, thread a new `pi_ollama: bool` (or equivalent) parameter into `render()` so direct CLI runs can opt into gateway discovery, not just the devcontainer variant
- [ ] 3.2 In `init-firewall.sh.j2`, add a new conditional block (separate from the existing all-ports `allow_host_network` rule) that, when Pi-Ollama is enabled: discovers the gateway IP (reuse existing `ip route` discovery code) and adds an iptables ACCEPT rule scoped to `tcp --dport 11434` for that IP
- [ ] 3.3 In the same block, pin `ollama.project-sandbox.internal` to the discovered gateway IP in `/etc/hosts`, reusing the existing `# project-sandbox-dns-pin` marker convention
- [ ] 3.4 Ensure `--no-firewall` continues to skip all firewall setup, including this new block, per existing precedent
- [ ] 3.5 Thread the new parameter through `cli.py`'s call to `firewall.render(...)` based on `args.pi_ollama and args.agent == "pi"`

## 4. Pi config baking

- [ ] 4.1 In `config_agents.py`, add a Pi-Ollama bake function (following the Claude/Codex `render()` pattern) producing `models.json` content: `providers.ollama` with `baseUrl: "http://ollama.project-sandbox.internal:11434/v1"`, `api: "openai-completions"`, `apiKey: "ollama"`, and a `models` list built from `--ollama-model` values or the default list (see 1.1/1.2)
- [ ] 4.2 Add `settings.json` content: `defaultProvider: "ollama"`, `defaultModel: <resolved default>`, plus `theme`/`lastChangelogVersion` fields per 1.5
- [ ] 4.3 Wire the bake into `render()`'s call sites so it only runs when `--pi-ollama` is set
- [ ] 4.4 Ensure no bake occurs, and no `/project-sandbox-config/pi` mount is created, when `--agent pi` is selected without `--pi-ollama` (existing behavior preserved)

## 5. Mount and provision plumbing

- [ ] 5.1 In `container_cli.py`'s `build_mount_specs()`/`build_run_argv()`, add a `/project-sandbox-config/pi` mount (new — none exists for Pi today), gated on `--pi-ollama`
- [ ] 5.2 In `_provision.sh.j2`, add a copy step placing the mounted `models.json` at `~/.pi/agent/models.json` and the mounted `settings.json` at `~/.pi/settings.json` (distinct target paths — the latter is a sibling of `~/.pi/agent`, not inside it)
- [ ] 5.3 Confirm the existing `~/.pi/agent/auth.json` credential sync (from `add-pi-agent-support`) is undisturbed by the new mount/copy steps

## 6. Docs

- [ ] 6.1 Update `docs/usage.md` to document `--pi-ollama` and `--ollama-model`, including the host-side prerequisite that Ollama must bind beyond `127.0.0.1` (e.g. `OLLAMA_HOST=0.0.0.0`) to be reachable via the gateway
- [ ] 6.2 Update `docs/security.md` to describe the new port-scoped gateway allow rule and its narrower scope relative to the existing devcontainer all-ports gateway rule
- [ ] 6.3 Update `docs/runtime.md` if the new mount/config paths affect documented file layout

## 7. Tests

- [ ] 7.1 `test_cli.py`: add tests for `--pi-ollama` parsing, its no-op behavior without `--agent pi`, and `--ollama-model` accumulation
- [ ] 7.2 `test_renderers.py`: assert the rendered firewall script contains the `tcp --dport 11434` gateway rule and the `ollama.project-sandbox.internal` `/etc/hosts` pin only when Pi-Ollama is enabled, and asserts their absence otherwise
- [ ] 7.3 Add/extend a `test_config_agents.py`-equivalent test asserting baked `models.json`/`settings.json` content matches the expected schema, including custom `--ollama-model` overrides
- [ ] 7.4 `test_container_cli.py`: assert the `/project-sandbox-config/pi` mount is present only when `--pi-ollama` is set, mirroring existing mount-presence tests for other agents
- [ ] 7.5 Regression test confirming `--pi-ollama` without `--agent pi` results in no firewall/config changes

## 8. Verification

- [ ] 8.1 `uv run python -m compileall src tests`
- [ ] 8.2 `uv run pytest -q`
- [ ] 8.3 `uv run project-sandbox --agent pi --pi-ollama --dry-run ...` to confirm rendered firewall script and mounts without starting a container
- [ ] 8.4 Manual end-to-end check with a real Ollama instance on the host (bound beyond loopback) across at least one runtime, confirming Pi can list/use Ollama models inside the container
