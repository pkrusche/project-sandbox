## 1. Confirmed unknowns

- 1.1 Default Ollama model ID list for `models.json` — not yet decided; pick a small, reasonable default set and make it easy to override.
- 1.2 `--ollama-model` flag shape — assume repeatable single-value (`action="append"`), mirroring `--extra-domain`.
- 1.3 `settings.json`'s `defaultModel` — assume it is simply the first entry of the resolved model list (defaults or user-supplied), unless a dedicated `--ollama-default-model` flag proves necessary during implementation.
- 1.4 Ollama port — hardcode `11434` for v1; no `--ollama-port` flag.
- 1.5 `settings.json`'s `lastChangelogVersion` — check whether Pi treats a stale/mismatched value as meaningful (e.g. triggers a changelog prompt); if not, pin to Pi's current npm version pin (`0.80.6`) for realism, otherwise omit the field.

## 2. CLI flags

- [x] 2.1 Add `--pi-ollama` flag to `cli.py` (`action="store_true"`, no-op unless `--agent pi` is also selected)
- [x] 2.2 Add `--ollama-model` flag to `cli.py` (`action="append"`, default `[]`), documented as only meaningful with `--pi-ollama`
- [x] 2.3 Validate/no-op gracefully when `--pi-ollama` or `--ollama-model` is passed without `--agent pi` (per spec: no Ollama-specific behavior triggered)

## 3. Firewall: port-scoped gateway access

- [x] 3.1 In `firewall.py`, thread a new `pi_ollama: bool` (or equivalent) parameter into `render()` so direct CLI runs can opt into gateway discovery, not just the devcontainer variant
- [x] 3.2 In `init-firewall.sh.j2`, add a new conditional block (separate from the existing all-ports `allow_host_network` rule) that, when Pi-Ollama is enabled: discovers the gateway IP (reuse existing `ip route` discovery code) and adds an iptables ACCEPT rule scoped to `tcp --dport 11434` for that IP
- [x] 3.3 In the same block, pin `ollama.project-sandbox.internal` to the discovered gateway IP in `/etc/hosts`, reusing the existing `# project-sandbox-dns-pin` marker convention
- [x] 3.4 Ensure `--no-firewall` continues to skip all firewall setup, including this new block, per existing precedent
- [x] 3.5 Thread the new parameter through `cli.py`'s call to `firewall.render(...)` based on `args.pi_ollama and args.agent == "pi"`

## 4. Pi config baking

- [x] 4.1 In `config_agents.py`, add a Pi-Ollama bake function (following the Claude/Codex `render()` pattern) producing `models.json` content: `providers.ollama` with `baseUrl: "http://ollama.project-sandbox.internal:11434/v1"`, `api: "openai-completions"`, `apiKey: "ollama"`, and a `models` list built from `--ollama-model` values or the default list (see 1.1/1.2)
- [x] 4.2 Add `settings.json` content: `defaultProvider: "ollama"`, `defaultModel: <resolved default>`, plus `theme`/`lastChangelogVersion` fields per 1.5
- [x] 4.3 Wire the bake into `render()`'s call sites so it only runs when `--pi-ollama` is set
- [x] 4.4 Ensure no bake occurs, and no `/project-sandbox-config/pi` mount is created, when `--agent pi` is selected without `--pi-ollama` (existing behavior preserved)

## 5. Mount and provision plumbing

- [x] 5.1 In `container_cli.py`'s `build_mount_specs()`/`build_run_argv()`, add a `/project-sandbox-config/pi` mount (new — none exists for Pi today), gated on `--pi-ollama`
- [x] 5.2 In `_provision.sh.j2`, add a copy step placing the mounted `models.json` at `~/.pi/agent/models.json` and the mounted `settings.json` at `~/.pi/settings.json` (distinct target paths — the latter is a sibling of `~/.pi/agent`, not inside it)
- [x] 5.3 Confirm the existing `~/.pi/agent/auth.json` credential sync (from `add-pi-agent-support`) is undisturbed by the new mount/copy steps

## 6. Docs

- [x] 6.1 Update `docs/usage.md` to document `--pi-ollama` and `--ollama-model` (superseded by 11.1 for the runtime-adapter design)
- [x] 6.2 Update `docs/security.md` to describe the new port-scoped gateway allow rule and its narrower scope relative to the existing devcontainer all-ports gateway rule
- [x] 6.3 Update `docs/runtime.md` if the new mount/config paths affect documented file layout

## 7. Tests

- [x] 7.1 `test_cli.py`: add tests for `--pi-ollama` parsing, its no-op behavior without `--agent pi`, and `--ollama-model` accumulation
- [x] 7.2 `test_renderers.py`: assert the rendered firewall script contains the `tcp --dport 11434` gateway rule and the `ollama.project-sandbox.internal` `/etc/hosts` pin only when Pi-Ollama is enabled, and asserts their absence otherwise
- [x] 7.3 Add/extend a `test_config_agents.py`-equivalent test asserting baked `models.json`/`settings.json` content matches the expected schema, including custom `--ollama-model` overrides
- [x] 7.4 `test_container_cli.py`: assert the `/project-sandbox-config/pi` mount is present only when `--pi-ollama` is set, mirroring existing mount-presence tests for other agents
- [x] 7.5 Regression test confirming `--pi-ollama` without `--agent pi` results in no firewall/config changes

## 8. Verification

- [x] 8.1 `uv run python -m compileall src tests`
- [x] 8.2 `uv run pytest -q`
- [x] 8.3 `uv run project-sandbox --agent pi --pi-ollama --dry-run ...` to confirm rendered firewall script and mounts without starting a container
- [x] 8.4 Manual end-to-end check with a real Ollama instance on the host (kept on loopback) across at least one runtime, confirming Pi can list/use Ollama models through the selected adapter

## 9. Review follow-ups

From the accuracy/correctness review of `main..add-local-ollama-support`:

- [x] 9.1 Fail loud on gateway-discovery failure: the `pi_ollama` block in `init-firewall.sh.j2` silently skips the allow rule and `/etc/hosts` pin when no default gateway is found (`if [ -n "$OLLAMA_HOST_GW4" ]` with no `else`), but `design.md`'s Risks section commits to "fail loud (clear error) rather than silently skipping". Add an `else`/`exit 1` branch (the block is only rendered when explicitly requested), or update `design.md` if silent-skip is now intended.
- [x] 9.2 Prevent `_PI_NPM_VERSION_PIN` drift: the constant in `config_agents.py` says "keep in sync" with the `pi-coding-agent` pin in `templates/Dockerfile.j2`, but `scripts/update-pins.py` only scans the Dockerfile template, so the next pin bump silently desynchronizes them (reintroducing Pi's changelog prompt). Add a test asserting the constant matches the version parsed from `Dockerfile.j2`, or parse the pin from the template at render time.
- [x] 9.3 Surface the `--no-firewall` + `--pi-ollama` interaction: both the port rule and the `ollama.project-sandbox.internal` hostname pin live in the firewall script, so with `--no-firewall` Pi's baked config points at a hostname nothing resolves. Add a CLI warning in `cli.py` and/or a sentence in `docs/usage.md`.
- [x] 9.4 Add a test that `cli.main` threads `pi_ollama=True` into `firewall.render(...)` — the existing dry-run CLI tests pass `--no-firewall`, so the one-line wiring in `cli.py` is currently untested end-to-end (the renderer itself is covered directly).
- [x] 9.5 Record resolutions for the open questions in section 1 (1.1–1.5): the implementation made all the calls (default model list, repeatable flag, first-model default, hardcoded port, `lastChangelogVersion` pinned) but no resolution is noted — in particular whether 1.5's "does Pi treat a stale `lastChangelogVersion` as meaningful" was actually confirmed against Pi's source.
- [x] 9.6 (Minor) Note the IPv4-only asymmetry: the neighboring `allow_host_network` block has an IPv6 gateway counterpart while the `pi_ollama` block is IPv4-only — fine in practice for Docker/Podman/Apple `container` gateways, but worth a one-line comment in the template to preempt the question.
- [x] 9.7 (Minor) Validate `--ollama-model` values: an empty string currently becomes `defaultModel: ""` in `settings.json`. JSON-only, no injection risk — reject empty/whitespace values with a friendly error.

## 10. Runtime networking adapter

- [x] 10.1 Research the supported runtimes and record the strategy matrix: Apple `container` localhost DNS; rootless Podman `pasta`/`host.containers.internal`; exact-bridge `socat` for local Linux Docker/rootful Podman; probe-gated aliases for Docker Desktop and Podman Machine
- [x] 10.2 Add a focused adapter that returns the fixed container hostname, verified endpoint, strategy kind, and optional owned-resource handle, or an actionable unsupported-mode error
- [x] 10.3 Implement Apple `container` localhost-DNS verification; never invoke `sudo` or mutate the mapping, and print the exact user-run setup command plus system-effect guidance when it is missing
- [x] 10.4 Implement rootless Podman native host mapping and fixed-hostname aliasing without starting a host listener
- [x] 10.5 Implement probe-gated native host mappings for Docker Desktop and Podman Machine; fail closed when physical-host loopback cannot be reached
- [x] 10.6 Implement local Linux Docker/rootful Podman bridge inspection and validate that the selected address is assigned, private, host-bindable, and never wildcard
- [x] 10.7 For the Linux bridge strategy only, require `socat`, start it on the exact bridge address forwarding to `127.0.0.1:11434`, and detect bind/immediate-child failure
- [x] 10.8 Pass the adapter-selected endpoint into fixed-hostname setup and the TCP/11434 firewall rule instead of unconditionally assuming the default gateway
- [x] 10.9 Integrate owned native-resource cleanup and proxy termination/reaping into normal exit, interruption, and container-start failure paths
- [x] 10.10 Ensure dry-run reports the selected/probed strategy without creating DNS mappings, listeners, or other state

## 11. Adapter documentation and tests

- [x] 11.1 Replace `OLLAMA_HOST=0.0.0.0` guidance with the runtime matrix, Apple user-run preconfiguration command and packet-filter/Private Relay effects, conditional `socat` prerequisite, unsupported-mode remediation, and residual private-network exposure
- [x] 11.2 Add unit tests for adapter selection, runtime command construction, endpoint validation, probe outcomes, unsupported modes, and dry-run non-mutation
- [x] 11.3 Add Apple preconfiguration verification tests proving the CLI never invokes `sudo` or creates/deletes mappings, plus forwarding lifecycle tests for normal exit, interruption, and container startup failure
- [x] 11.4 Add Linux bridge proxy tests for missing `socat`, unsafe/occupied addresses, immediate proxy exit, cleanup, and the absence of wildcard binds
- [x] 11.5 Add regressions proving no forwarding setup occurs when `--pi-ollama` is absent or ineffective for a non-Pi agent
- [x] 11.6 Run compileall, the full pytest suite, strict OpenSpec validation, and dry-run previews for every supported runtime strategy
- [x] 11.7 Complete an end-to-end test for each claimed runtime mode with Ollama listening only on `127.0.0.1:11434`; record probe-gated or unsupported modes explicitly
