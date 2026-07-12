## Context

project-sandbox has no shared agent registry: each of the three supported agents (Claude, Codex, OpenCode) is hardcoded by name across `cli.py`, `container_cli.py`, `config_agents.py`, `dockerfile.py`/`Dockerfile.j2`, `devcontainer.py`, `entrypoint.sh.j2`/`_provision.sh.j2`, `oauth_refresh.py`, and `token_expiry.py`. Adding a new agent is a mechanical, repeated pattern of touching each file's per-agent branch/dict/comprehension.

Pi's shape sits between the two existing BYOK-flat-file patterns:
- Like **Codex**: credentials are a single flat file (`~/.pi/agent/auth.json`, mode 0600 — API key string or OAuth token object), not a directory tree.
- Like **OpenCode**: no baked config file to render/mount, BYOK by default (20+ providers), so firewall-allowlist warnings apply and there's no VS Code extension angle.
- Unlike either: **Pi has no built-in permission/confirmation system**. The project's own note is "run in a container, or build your own confirmation flow with extensions" — meaning there is no bypass-permissions config file to bake in, only env vars and the `--approve`/`-a` CLI flag (trusts project-local `.pi/` config for one run).

## Goals / Non-Goals

**Goals:**
- Run Pi headless and interactively inside the existing container/firewall model, reusing the Codex-credentials + OpenCode-no-config patterns rather than inventing a third.
- Keep the change mechanical and low-risk: touch each file the same way OpenCode's addition touched it, verified by grepping for every `"opencode"` occurrence.
- Ship without OAuth refresh / token-expiry wiring for Pi, since both modules degrade safely for unknown agents.

**Non-Goals:**
- Do not implement `oauth_refresh.py` delegation or `token_expiry.py` parsing for Pi in this change — the exact JSON shape of an OAuth entry in `auth.json` (flat like Codex's vs. provider-keyed like OpenCode's) and whether a `pi login status`-equivalent subcommand exists are unconfirmed against Pi's actual source. Guessing wrong field names is worse than shipping without them, since a wrong guess could silently misreport token freshness rather than no-op.
- Do not build a custom permission/confirmation system for Pi. `--approve` is the accepted trust boundary for headless mode; the container's own firewall/sandboxing is the safety net, consistent with the project's existing "run in a container" posture for BYOK agents.
- Do not add a markdown transcript renderer for Pi (no structured JSON output from headless mode to render).

## Decisions

**1. Credential sync: reuse `_sync_generic_credentials`, not a new Pi-specific sync function.**
Codex already calls `_sync_generic_credentials(project_sandbox_dir, "codex", source_dir, include_files=("auth.json",))` (`config_agents.py:115-120`) for its single flat `auth.json`. Pi's credential file is the same shape (`~/.pi/agent/auth.json`), so add a parallel call gated on `host_paths["pi"].exists()`, mirroring the Codex branch rather than OpenCode's `_sync_opencode_credentials` (which handles a multi-directory tree OpenCode has and Pi doesn't).
*Alternative considered*: generalize `_sync_generic_credentials`'s caller into a loop over an agent→include_files map. Rejected for this change to keep the diff mechanical and reviewable against the existing if/elif-per-agent style; a registry-style refactor is a separate, larger change (see Open Questions).

**2. Config/secrets mount split: secrets-only, no `/project-sandbox-config/pi`.**
Codex mounts both a config dir (`/project-sandbox-config/codex`, rendered `config.toml`) and secrets (`/project-sandbox-secrets/codex`). OpenCode mounts secrets only, no baked config, because it has no host-renderable config file. Pi likewise has no config file to render (auth is env var / OAuth `/login` / BYOK, and permission behavior isn't config-driven) — so follow OpenCode: `/project-sandbox-secrets/pi` only, in `build_mount_specs` (`container_cli.py:152`) and `build_run_argv` (`container_cli.py:259`).

**3. Provider-allowlist warning: generalize `_warn_opencode_provider_allowlist`, don't duplicate it.**
The existing function (`cli.py:1068`) is opencode-specific by name and by the `run_agent == "opencode"` check. Since Pi has the identical BYOK-firewall problem (unknown provider domains aren't allowlisted by default), rename/generalize it to trigger on `run_agent in ("opencode", "pi")` with agent-appropriate wording, rather than writing a second near-identical function. Call sites (`cli.py:335`, `cli.py:637`) stay the same, just pass through.
*Alternative considered*: leave `_warn_opencode_provider_allowlist` untouched and add a sibling `_warn_pi_provider_allowlist`. Rejected — the warning text and trigger condition are identical in substance (BYOK provider ⇒ check firewall allowlist), and duplicating it invites drift if the warning copy changes later.

**4. Headless invocation and flag translation.**
`entrypoint.sh.j2` gets `pi)` → `exec pi "$@"` and `pi-headless)` → `exec pi -p "$PROMPT" --approve "$@"`, following the `codex`/`codex-headless` and `opencode`/`opencode-headless` case-arm pattern (`entrypoint.sh.j2:75-124`). `--approve` is always passed for headless (never conditionally), because headless mode cannot answer an interactive trust prompt and Pi has no other way to grant it. Model/effort injection for headless runs must emit Pi's single combined flag (`--model sonnet:high`) rather than the two separate `--model`/`--effort` flags Codex and OpenCode use — this is a genuine branch-point in the CLI's flag-building logic, not just a name swap.

**5. Telemetry/update-check suppression via env vars, not a config file.**
Set `PI_SKIP_VERSION_CHECK=1` in the entrypoint/provision script env. The default setting of pi without plugins should not send telemetry, but add an option for --offline or PI_OFFLINE=1 to disable all startup network operations described here, including update checks, package update checks, and install/update telemetry. 

**6. Defer `oauth_refresh.py` / `token_expiry.py`.**
Both modules are structured as agent-keyed dispatch (dict lookups, e.g. `oauth_refresh.py:25`, `token_expiry.py:147-148`), not if/elif chains, and both already handle unknown agents by silent no-op / returning `None`. Leaving `"pi"` out of these dicts is therefore safe by construction — no crash, just no refresh/expiry-reporting capability for Pi yet. Follow up once Pi's `auth.json` OAuth shape and refresh subcommand (if any) are confirmed.

## Risks / Trade-offs

- **[Risk]** Generalizing `_warn_opencode_provider_allowlist` touches an existing, tested code path shared with OpenCode. **Mitigation**: existing OpenCode tests around this function must still pass unchanged; add Pi-specific test cases alongside rather than replacing OpenCode's.
- **[Risk]** No `oauth_refresh.py`/`token_expiry.py` support means Pi OAuth users get no automatic refresh or expiry warnings, unlike Codex/Claude users. **Mitigation**: explicitly called out as deferred in proposal.md and this design; not a silent gap.
- **[Trade-off]** Not generalizing the per-agent if/elif/comprehension pattern into a registry (Decision 1's alternative) means this change's diff touches ~10 files with small, repetitive edits, and a fifth future agent will repeat the same shape of diff again. Accepted for now to keep this change's blast radius small and match the existing codebase style; worth revisiting if a fifth agent is ever proposed.

## Open Questions

- Does Pi expose a `pi login status`-equivalent subcommand for `oauth_refresh.py` to delegate to? (Blocks future refresh wiring, not this change.)
- Is a Pi OAuth entry in `auth.json` flat (like Codex) or provider-keyed (like OpenCode, since Pi is also multi-provider)? (Blocks future `token_expiry.py` wiring, not this change.)
- Should the per-agent hardcoding pattern (10+ touch points per agent) be replaced with a registry now that this is the fourth agent added the same way? Flagged as a candidate follow-up change, out of scope here.
