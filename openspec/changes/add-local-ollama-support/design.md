## Context

Pi (`add-pi-agent-support`) is BYOK: it can reach any provider the firewall allowlists, but the sandboxed container runs in its own network namespace under every supported runtime (Apple `container`, Docker, Podman — `container_cli.py` never passes `--network host`). `localhost` inside the container is always the container's own loopback; there is no `host.docker.internal`-style alias anywhere in this codebase. A host-run Ollama server (default bind `127.0.0.1:11434`) is therefore unreachable from inside the sandbox regardless of firewall policy — this is a network-topology problem, not just an allowlist gap.

A partial building block already exists: `init-firewall.sh.j2` discovers the container's default-gateway IP via `ip route` and allowlists it, but only for the devcontainer variant (`allow_host_network=True`, gated in `firewall.py`), only for IDE port-forwarding, and as an all-ports allow to that one IP. The regular per-domain allow loop in the same template resolves each domain once via `dig`, adds the resolved IP to an ipset, and pins the hostname into `/etc/hosts` with a `# project-sandbox-dns-pin` marker.

## Goals / Non-Goals

**Goals:**
- Let `--agent pi --pi-ollama` reach a host-run Ollama server from inside the sandboxed container, for any of the three supported runtimes.
- Pre-configure Pi (`~/.pi/agent/models.json`, `~/.pi/settings.json`) so Ollama works as the default provider with zero manual setup inside the container.
- Keep the new firewall exposure as narrow as possible: only the Ollama port, not general host-network access.
- No behavior change for anyone not passing `--pi-ollama`.

**Non-Goals:**
- Generalizing this pattern to OpenCode or other agents (a reasonable future follow-up; OpenCode's config-sync pattern would need its own design pass).
- An `OLLAMA_HOST` env var or any other runtime-discoverable address mechanism — superseded by the fixed-hostname approach.
- Transparent redirection of literal `localhost:11434` traffic (DNAT/REDIRECT) — considered and rejected; an explicit hostname is simpler and matches how every other allowlisted endpoint is already addressed in this codebase.
- Making the Ollama port configurable in v1 (see Open Questions).
- Verifying or managing the host's own Ollama configuration (e.g. `OLLAMA_HOST=0.0.0.0` on the host) — that's a host-side prerequisite to document, not something this change can enforce.

## Decisions

### Extend the existing gateway-allow mechanism, don't build a new one
`allow_host_network`'s gateway-discovery code in `init-firewall.sh.j2` already does 90% of what's needed. Rather than adding a parallel mechanism, extend it: (a) let direct CLI runs opt into gateway discovery too (today it's devcontainer-only), driven by `--pi-ollama` rather than the devcontainer/direct-run split; (b) add a second, narrower rule alongside the existing all-ports gateway allow: `-p tcp --dport 11434 -d $HOST_GW -j ACCEPT`. The existing all-ports devcontainer rule is left untouched — this is an additive, port-scoped rule for the CLI path.

*Alternative considered*: a wholly separate "Ollama forwarding" script/mechanism. Rejected — it would duplicate gateway-discovery logic that already exists and is already tested via the devcontainer path.

### Fixed hostname (`ollama.project-sandbox.internal`) pinned via the existing DNS-pin mechanism, not an env var
Because the hostname is fixed and known at CLI-invocation time (unlike the gateway IP, which is only discoverable inside the container's own network namespace at startup), Pi's `models.json` can be baked host-side with the hostname baked in literally. Only the `/etc/hosts` entry (hostname → discovered IP) needs to happen at runtime — and that's the same mechanism already used for every other allowlisted domain, just sourced from `ip route` instead of `dig`.

*Alternative considered*: bake a placeholder into `models.json` and patch it with the real IP via `sed`/`jq` in the entrypoint after gateway discovery. Rejected once the fixed-hostname option was on the table — it needs no runtime file mutation at all, which is strictly simpler and lower-risk (no chance of a patch step silently failing and leaving a broken config).

*Alternative considered*: `OLLAMA_HOST` env var set by the entrypoint after gateway discovery, read by Pi at runtime. Rejected — requires Pi to consult an env var independently of `models.json`/`settings.json` (unconfirmed, and an extra moving part with no benefit over baking the hostname directly into config that's already being written).

### New Pi config-bake path, following the Claude/Codex pattern
`config_agents.py` already has two patterns: `render()` bakes fresh JSON host-side (Claude, Codex); `_sync_opencode_credentials`-style functions instead copy existing host files (OpenCode). Pi currently has neither baked config (per `add-pi-agent-support`'s explicit "BYOK, no host-renderable config" decision) — only synced `auth.json` credentials. This change adds a bake path for Pi specifically when `--pi-ollama` is set, following the Claude/Codex shape since there's nothing on the host to sync from; the config is synthesized entirely by project-sandbox.

`~/.pi/settings.json` lives one level above the currently-mounted `~/.pi/agent/` — a new mount target and a new `_provision.sh.j2` copy step are needed to place it correctly, alongside the existing `models.json` write under `~/.pi/agent/`.

### Port-scoping over blanket gateway access
Every existing firewall allow-rule in this codebase permits all ports to an allowlisted IP (this is fine for public API endpoints reached over 443). Extending that same all-ports shape to the host gateway for direct CLI runs would expose every host-bound service, not just Ollama — a materially larger attack surface than the feature calls for. The new rule is scoped to `tcp --dport 11434`, a small, deliberate divergence from the existing all-ports pattern, justified by the fact that this is the first rule reaching the *host* rather than a public internet endpoint.

## Risks / Trade-offs

- **[Risk]** The port-scoped gateway rule is new territory for `init-firewall.sh.j2` (every existing rule is IP-only). A bug in the iptables rule syntax could silently over- or under-scope access. → **Mitigation**: dedicated test asserting the rendered firewall script contains the `--dport 11434` restriction and does not grant broader access when `--pi-ollama` is set alone (i.e., without `allow_host_network`'s devcontainer IDE-forwarding rule also being active).
- **[Risk]** If Ollama on the host is bound only to `127.0.0.1` (the default), the gateway route will not reach it regardless of firewall configuration — this change cannot fix that, since it's host-side. → **Mitigation**: document the `OLLAMA_HOST=0.0.0.0` (or equivalent bind-address) prerequisite prominently in `docs/usage.md`/`docs/security.md`.
- **[Risk]** Gateway-IP discovery behavior may differ subtly across Apple `container`, Docker, and Podman network backends (untested for this specific purpose — the existing devcontainer use of this code only exercises it for IDE port-forwarding, not for reaching an arbitrary host service on a specific port). → **Mitigation**: manual verification across runtimes called out in tasks.md; if a runtime's gateway route can't be discovered reliably, fail loud (clear error) rather than silently skipping the allow rule.
- **[Trade-off]** Baking `models.json`/`settings.json` from scratch overwrites any existing Pi provider config a user might already have inside the container from a previous run or manual edit. Given these files are written fresh into a project-scoped mount path each run (matching the Claude/Codex bake pattern, which already has this property), this is consistent with existing behavior, not a new risk class — but worth a one-line callout in docs since it's a new surface for Pi specifically.

## Open Questions

- Default Ollama model ID list to ship in `models.json`, and the exact shape of `--ollama-model` (repeatable single-value flag, mirroring `--extra-domain`, is the working assumption) — not yet decided.
- Whether `settings.json`'s `defaultModel` is simply the first entry of the model list, or independently settable via its own flag.
- Whether the Ollama port (11434) should be user-overridable via a flag in a future iteration, or remain hardcoded (current lean: hardcode for v1, matching Ollama's near-universal default).
- Whether `settings.json`'s `lastChangelogVersion` field should track Pi's pinned npm version (currently `0.80.6`, maintained via `scripts/update-pins.py`) or can be static/omitted — needs checking whether Pi treats a stale value as meaningful (e.g. triggers a changelog prompt) or ignores it.
