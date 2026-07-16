## Context

Pi (`add-pi-agent-support`) is BYOK: it can reach any provider the firewall allowlists, but the sandboxed container runs in its own network namespace under every supported runtime (Apple `container`, Docker, Podman — `container_cli.py` never passes `--network host`). `localhost` inside the container is always the container's own loopback; there is no portable host alias in this codebase. A host-run Ollama server (default bind `127.0.0.1:11434`) is therefore unreachable from inside the sandbox regardless of firewall policy — this is a network-topology problem, not just an allowlist gap. Binding Ollama to `0.0.0.0` solves reachability but unnecessarily publishes its unauthenticated API on every host interface.

A partial building block already exists: `init-firewall.sh.j2` discovers the container's default-gateway IP via `ip route` and allowlists it, but only for the devcontainer variant (`allow_host_network=True`, gated in `firewall.py`), only for IDE port-forwarding, and as an all-ports allow to that one IP. The regular per-domain allow loop in the same template resolves each domain once via `dig`, adds the resolved IP to an ipset, and pins the hostname into `/etc/hosts` with a `# project-sandbox-dns-pin` marker.

## Goals / Non-Goals

**Goals:**
- Let `--agent pi --pi-ollama` reach a host-run Ollama server from inside the sandboxed container, for any of the three supported runtimes.
- Keep Ollama bound to host loopback and expose only a short-lived proxy on the runtime-private interface used by the sandbox.
- Pre-configure Pi (`~/.pi/agent/models.json`, `~/.pi/settings.json`) so Ollama works as the default provider with zero manual setup inside the container.
- Keep the new firewall exposure as narrow as possible: only the Ollama port, not general host-network access.
- No behavior change for anyone not passing `--pi-ollama`.

**Non-Goals:**
- Generalizing this pattern to OpenCode or other agents (a reasonable future follow-up; OpenCode's config-sync pattern would need its own design pass).
- An `OLLAMA_HOST` env var or any other runtime-discoverable address mechanism — superseded by the fixed-hostname approach.
- Transparent redirection of literal `localhost:11434` traffic (DNAT/REDIRECT) — considered and rejected; an explicit hostname is simpler and matches how every other allowlisted endpoint is already addressed in this codebase.
- Making the Ollama port configurable in v1 (see Open Questions).
- Reconfiguring Ollama's bind address; loopback-bound Ollama is the intended configuration.

## Decisions

### Use a runtime networking adapter, preferring native loopback forwarding
Keep Ollama on `127.0.0.1:11434`. A focused adapter selects and verifies the forwarding strategy before container startup while preserving the fixed Pi-facing hostname:

- **Apple `container`**: use its localhost DNS facility (`container system dns ... --localhost`) to map `ollama.project-sandbox.internal` through the runtime to macOS loopback. Treat its documented privilege, packet-filter, restart, and Private Relay effects as explicit prerequisites/trade-offs. Track whether this invocation created the mapping and remove only mappings it owns.
- **Rootless Podman on Linux**: use the runtime-provided `host.containers.internal` path backed by `pasta`'s guest-address mapping. Alias or resolve the fixed Pi hostname to the verified native endpoint; do not start a host listener.
- **Docker Desktop and Podman Machine**: use their VM-provided host aliases only after a loopback reachability probe confirms the selected runtime/version forwards to the physical host. A DNS result alone is insufficient. If the probe fails, report the mode as unsupported rather than exposing a wildcard listener.
- **Local Linux Docker and rootful Podman bridge modes**: discover the actual host bridge address from runtime network inspection, verify it is assigned and bindable on the host, then run `socat` on that exact address and port 11434 forwarding to `127.0.0.1:11434`.

The adapter returns both the container-visible endpoint and an optional owned-resource handle. The firewall consumes that endpoint rather than assuming the default gateway. The outer CLI lifecycle removes owned native mappings and terminates/reaps a managed `socat` child after normal exit, interruption, or container-start failure. Existing session cleanup remains responsible for the container.

*Alternative considered*: expose Ollama itself with `OLLAMA_HOST=0.0.0.0`. Rejected because it publishes Ollama on LAN, VPN, and other host interfaces and gives the sandbox feature control over a separately managed service.

*Alternative considered*: use `socat` for every runtime. Rejected because VM-backed and rootless runtimes expose synthetic container endpoints that are not necessarily host-bindable addresses, while their native forwarding mechanisms already reach host loopback safely.

*Alternative considered*: netcat for Linux bridge forwarding. Rejected because macOS/BSD and Linux netcat variants have incompatible forwarding options, and common forms do not reliably provide concurrent, long-lived HTTP forwarding or uniform child cleanup. `socat` has explicit bind, fork, and reuse-address behavior suitable for the fallback.

*Alternative considered*: an unmanaged user-started proxy. Rejected as the primary path because it adds setup and makes cleanup and safe binding easy to get wrong; the documented troubleshooting path may still show the equivalent `socat` command.

### Generalize the existing port-scoped gateway rule to an adapter-selected endpoint
The existing `--dport 11434` rule remains the container-side security boundary, but its destination is supplied or resolved from the selected adapter rather than always inferred from `ip route`. The fixed hostname is pinned where compatible with the runtime; native DNS mappings remain authoritative where the runtime requires them. The existing all-ports devcontainer rule is untouched.

### Fixed hostname (`ollama.project-sandbox.internal`) pinned via the existing DNS-pin mechanism, not an env var
Because the hostname is fixed and known at CLI-invocation time, Pi's `models.json` can be baked host-side with it literally. The runtime adapter decides how that hostname resolves: an owned native DNS mapping, an alias to a runtime-provided host endpoint, or an `/etc/hosts` pin to a verified bridge proxy. Pi configuration remains independent of those runtime details.

*Alternative considered*: bake a placeholder into `models.json` and patch it with the real IP via `sed`/`jq` in the entrypoint after gateway discovery. Rejected once the fixed-hostname option was on the table — it needs no runtime file mutation at all, which is strictly simpler and lower-risk (no chance of a patch step silently failing and leaving a broken config).

*Alternative considered*: `OLLAMA_HOST` env var set by the entrypoint after gateway discovery, read by Pi at runtime. Rejected — requires Pi to consult an env var independently of `models.json`/`settings.json` (unconfirmed, and an extra moving part with no benefit over baking the hostname directly into config that's already being written).

### New Pi config-bake path, following the Claude/Codex pattern
`config_agents.py` already has two patterns: `render()` bakes fresh JSON host-side (Claude, Codex); `_sync_opencode_credentials`-style functions instead copy existing host files (OpenCode). Pi currently has neither baked config (per `add-pi-agent-support`'s explicit "BYOK, no host-renderable config" decision) — only synced `auth.json` credentials. This change adds a bake path for Pi specifically when `--pi-ollama` is set, following the Claude/Codex shape since there's nothing on the host to sync from; the config is synthesized entirely by project-sandbox.

`~/.pi/settings.json` lives one level above the currently-mounted `~/.pi/agent/` — a new mount target and a new `_provision.sh.j2` copy step are needed to place it correctly, alongside the existing `models.json` write under `~/.pi/agent/`.

### Port-scoping over blanket gateway access
Every existing firewall allow-rule in this codebase permits all ports to an allowlisted IP (this is fine for public API endpoints reached over 443). Extending that same all-ports shape to the host gateway for direct CLI runs would expose every host-bound service, not just Ollama — a materially larger attack surface than the feature calls for. The new rule is scoped to `tcp --dport 11434`, a small, deliberate divergence from the existing all-ports pattern, justified by the fact that this is the first rule reaching the *host* rather than a public internet endpoint.

## Risks / Trade-offs

- **[Risk]** The port-scoped gateway rule is new territory for `init-firewall.sh.j2` (every existing rule is IP-only). A bug in the iptables rule syntax could silently over- or under-scope access. → **Mitigation**: dedicated test asserting the rendered firewall script contains the `--dport 11434` restriction and does not grant broader access when `--pi-ollama` is set alone (i.e., without `allow_host_network`'s devcontainer IDE-forwarding rule also being active).
- **[Risk]** Native forwarding or a bridge listener can expose Ollama to other peers that can reach the runtime network. → **Mitigation**: keep the container rule port-scoped, bind fallback proxies only to the exact bridge address, scope native mappings to the fixed hostname where supported, and document each runtime's residual trust boundary.
- **[Risk]** A runtime alias may resolve without actually forwarding to physical-host loopback. → **Mitigation**: require an end-to-end reachability probe for VM-backed modes; DNS presence alone does not select an adapter.
- **[Risk]** Apple `container` localhost DNS changes host packet-filter state and may disable Private Relay. → **Mitigation**: surface these effects before setup, distinguish pre-existing from owned mappings, remove only owned mappings, and provide exact remediation when privilege or system configuration prevents setup.
- **[Risk]** Abrupt CLI termination could orphan `socat`. → **Mitigation**: use a dedicated process group where supported, install cleanup for normal and signal paths, detect stale/listening conflicts on the next run, and always reap the child.
- **[Trade-off]** `socat` is a new host prerequisite for some optional runtime modes. → **Mitigation**: check it only after the adapter selects the local Linux bridge fallback and emit an actionable installation error.
- **[Risk]** Gateway-IP discovery behavior may differ subtly across Apple `container`, Docker, and Podman network backends (untested for this specific purpose — the existing devcontainer use of this code only exercises it for IDE port-forwarding, not for reaching an arbitrary host service on a specific port). → **Mitigation**: manual verification across runtimes called out in tasks.md; if a runtime's gateway route can't be discovered reliably, fail loud (clear error) rather than silently skipping the allow rule.
- **[Trade-off]** Baking `models.json`/`settings.json` from scratch overwrites any existing Pi provider config a user might already have inside the container from a previous run or manual edit. Given these files are written fresh into a project-scoped mount path each run (matching the Claude/Codex bake pattern, which already has this property), this is consistent with existing behavior, not a new risk class — but worth a one-line callout in docs since it's a new surface for Pi specifically.

## Open Questions

- Default Ollama model ID list to ship in `models.json`, and the exact shape of `--ollama-model` (repeatable single-value flag, mirroring `--extra-domain`, is the working assumption) — not yet decided.
- Whether `settings.json`'s `defaultModel` is simply the first entry of the model list, or independently settable via its own flag.
- Whether the Ollama port (11434) should be user-overridable via a flag in a future iteration, or remain hardcoded (current lean: hardcode for v1, matching Ollama's near-universal default).
- Which Docker Desktop and Podman Machine versions reliably forward their native host aliases to services bound only on physical-host loopback; support remains probe-gated until verified.
- Whether `settings.json`'s `lastChangelogVersion` field should track Pi's pinned npm version (currently `0.80.6`, maintained via `scripts/update-pins.py`) or can be static/omitted — needs checking whether Pi treats a stale value as meaningful (e.g. triggers a changelog prompt) or ignores it.

### Resolutions

- Default model list: `qwen2.5-coder`, `llama3.1`, `deepseek-coder-v2` (`config_agents.DEFAULT_OLLAMA_MODELS`) — a small, popular-model set covering general coding use. `--ollama-model` is repeatable (`action="append"`), matching `--extra-domain`, and fully overrides (not appends to) the default list when given.
- `defaultModel` is the first entry of the resolved model list (first `--ollama-model` given, or the first default). No separate `--ollama-default-model` flag was added — not needed given `--ollama-model`'s order already controls this.
- Ollama port is hardcoded to `11434`, no `--ollama-port` flag in v1, per the original lean.
- `lastChangelogVersion` is pinned to `config_agents._PI_NPM_VERSION_PIN` (kept manually in sync with the `pi-coding-agent` npm pin in `templates/Dockerfile.j2`; a regression test — `tests/test_update_pins.py::NpmPinUpdateTests::test_config_agents_pi_pin_matches_dockerfile_template` — now fails if the two drift). Whether Pi actually treats a stale value as meaningful (e.g. triggers a changelog prompt) was **not** confirmed against Pi's source — pinning to the known npm version is the safer default regardless, since it's truthful rather than a guess, but this remains unverified.
- Runtime strategy: Apple `container` and rootless Podman use native loopback forwarding; local Linux Docker/rootful Podman use an exact-bridge `socat` fallback; Docker Desktop and Podman Machine are enabled only after a loopback reachability probe succeeds.
