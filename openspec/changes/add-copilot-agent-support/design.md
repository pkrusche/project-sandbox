## Context

Agents are hardcoded by name across `cli.py`, `config_agents.py`, `container_cli.py`, `dockerfile.py`/`Dockerfile.j2`, the entrypoint and provision templates, `transcript.py`, and the firewall. The Pi change (`archive/2026-07-12-add-pi-agent-support`) is the template for these edits.

Copilot is different from the existing agents in three ways:

1. **The credential is a GitHub token, not an agent-specific credential.** On macOS, `copilot login` stores its token in the Keychain, so nothing useful sits in `~/.copilot/`. Copilot reads `COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`, then its own credential store, which inside a container with no Keychain is a plaintext file under `$COPILOT_HOME`. It accepts fine-grained PATs with the "Copilot Requests" permission and OAuth tokens from the Copilot CLI or GitHub CLI apps. It does not accept classic `ghp_` PATs.
2. **The account type affects the network.** Personal, Business, and Enterprise accounts on github.com use `api.{individual,business,enterprise}.githubcopilot.com`, which the existing `--allow-github` list already includes. GHE.com tenants use per-tenant hosts under `<tenant>.ghe.com`.
3. **The bash heuristic already exists.** `cli.py::_uses_github_copilot_cli` widens the firewall based on the first word of a bash prompt.

Relevant CLI surface (from GitHub's Copilot CLI reference): `-p`, `--allow-all` (= `--allow-all-tools --allow-all-paths --allow-all-urls`), `--no-ask-user`, `--output-format json` (JSONL), `-s`, `--model`, `--effort`/`--reasoning-effort` (`low|medium|high|xhigh|max`), `--no-auto-update`, `--disable-builtin-mcps`, `--log-dir`. Environment variables: `COPILOT_HOME`, `COPILOT_GH_HOST`, `COPILOT_AUTO_UPDATE`, `COPILOT_ALLOW_ALL`. The reference does not document the JSONL event schema or exit codes for failed runs.

## Goals / Non-Goals

**Goals:**
- `--agent copilot` works on a Copilot subscription for personal, Business, and GHEC accounts on github.com, and for GHEC with data residency (`*.ghe.com`).
- Prefer a narrowly scoped PAT from `pass`. Fall back to `gh auth token` with an explicit warning.
- The token reaches only the Copilot process. It is never baked into the image, never written into the repo, never in argv, and never in bash sessions.
- Turning on GitHub network access is an explicit decision: the agent choice or `--allow-github`, never prompt text.
- Behaviour that can't be checked here (JSONL shape, exit codes, trusted-folder seeding, tenant hosts) is covered by an opt-in e2e suite that a personal account can run, with the tenant checks optional.

**Non-Goals:**
- Self-hosted GHES. Rejected by host validation and recorded in TODO.md.
- Staging the token from the macOS Keychain. Deferred to TODO.md.
- Devcontainer support for Copilot. Deferred: a token captured when the devcontainer is generated would go stale.
- BYOK or agent-gateway routing for Copilot (`COPILOT_PROVIDERS_CONFIG`). This change is subscription-only, and `--agent-proxy` is rejected.
- OAuth refresh or token-expiry reporting. Fetching the token fresh at every launch makes them unnecessary.
- An agent registry refactor. This change keeps the per-agent style.

## Decisions

**1. Host model: `--copilot-host`, restricted to github.com or `*.ghe.com`.**
The flag defaults to `github.com`. A value must be exactly `github.com` or match `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com$`. Anything else exits with a message that GHES isn't supported. Personal, Business, and GHEC-on-github.com users need no flag, because the Copilot endpoint for their account type is already in the `--allow-github` set. Inside the container, `COPILOT_GH_HOST` is set only for a tenant host. It is preferred over `GH_HOST` because it applies to Copilot alone.
*Alternatives considered:* (a) discover the endpoint at launch from an account-info API. Rejected: the endpoint is undocumented, needs network access and a token before the firewall is rendered, and conflicts with the requirement that `--dry-run` do no lookups. (b) accept any host. Rejected: it would quietly let GHES fail, and every host widens the firewall.

**2. Firewall: Copilot implies `--allow-github`, plus tenant hosts for GHE.com.**
`_allow_github()` becomes `args.allow_github or run_agent == "copilot"`. When `--copilot-host` is a tenant, `firewall.render()` gets a new `copilot_tenant_hosts` list, rendered from a fixed per-tenant set (`<tenant>.ghe.com`, `api.<tenant>.ghe.com`, `copilot-api.<tenant>.ghe.com`, `copilot-telemetry-service.<tenant>.ghe.com`). These hosts are DNS-pinned like the existing GitHub hosts. Before implementation, check the exact set against GitHub's published Copilot allowlist reference for data residency (task 1.1); the e2e tenant check then confirms it.

*Task 1.1 result (2026-11-02):* GitHub's allowlist reference (https://docs.github.com/en/copilot/reference/copilot-allowlist-reference, "GHE.com" section) lists `SUBDOMAIN.ghe.com` and the wildcard `*.SUBDOMAIN.ghe.com`. The firewall pins resolved IPs per hostname and cannot express a wildcard, so the pinned set is the concrete per-tenant hosts Copilot CLI uses: `<tenant>.ghe.com`, `api.<tenant>.ghe.com` (REST and Copilot token exchange), `copilot-api.<tenant>.ghe.com` (inference), and `copilot-telemetry-service.<tenant>.ghe.com`. The per-service names come from GitHub's gh-aw GHE.com troubleshooting guide (https://github.github.com/gh-aw/troubleshooting/debug-ghe/), because the allowlist page only gives the wildcard. The reference also mentions `copilot-proxy.SUBDOMAIN.ghe.com`, which IDE completions use. It is not pinned, because the CLI does not need it. No `github.com` or `*.githubcopilot.com` host is tenant-specific. The shared `api.githubcopilot.com` and the telemetry host are already in the `--allow-github` set, which Copilot implies. The `tenant` e2e check confirms the set, and `--extra-domain` covers any gap. The hosts go only into the sandbox firewall script. The devcontainer variant isn't used by Copilot.
`_uses_github_copilot_cli` is deleted. This is a breaking change for `--agent bash --prompt-text 'copilot …'`, which now needs `--allow-github` and brings its own token. It goes in CHANGELOG.

**3. Token resolution in a new module, `copilot_auth.py`.**
`resolve_token(host, pass_entry, explicit_entry) -> (token, source)`:
1. `pass show <entry>` with a timeout. The default entry is `copilot-github-token` for github.com and `copilot-github-token/<host>` for a tenant, so a github.com PAT is never sent to a tenant. Use the first line of output, stripped. If `pass` is missing, or the default entry is missing, continue to step 2 quietly. If the user named an entry with `--copilot-token-pass` and the lookup fails, that is an error: no silent fallback.
2. `gh auth token --hostname <host>`. On success the source is `gh`, and the CLI prints `[W]`: the agent holds your GitHub CLI token, which may have repo or workflow scope, while GitHub is reachable; see docs/security.md; store a fine-grained PAT with only "Copilot Requests" in `pass` at `<entry>` to narrow it.
3. Otherwise exit, naming both remedies: `pass insert <entry>` and `gh auth login --hostname <host>`.

Reject tokens that start with `ghp_`, with a message saying classic PATs aren't supported. The resolved value goes into the existing secret-redaction set. `--dry-run` skips resolution and shows a placeholder source.
*Alternatives considered:* an env-var source such as `COPILOT_GITHUB_TOKEN` on the host. Left out to keep the surface small and in line with "prefer mounted credential files", and easy to add later. A raw `--copilot-token` flag. Rejected for argv and history exposure, as agent-proxy documents.

**4. Token staging and delivery.**
After resolution, write the token to `config_agents.credentials_dir(project_sandbox_dir, "copilot")/token` (mode 0600, directory 0700). This is outside the repo and is rewritten on every launch. Remove it best-effort when the session ends. `container_cli.build_mount_specs()` mounts it read-only at `/project-sandbox-secrets/copilot`, but only when `run_agent == "copilot"`. It is not mounted for bash sessions or any other agent: this deliberately breaks the "multi-agent bash" convention. The `copilot)` and `copilot-headless)` entrypoint arms read it with `COPILOT_GITHUB_TOKEN="$(cat /project-sandbox-secrets/copilot/token)"` and export it only for the `exec`. Provisioning never copies the token into `$COPILOT_HOME`, so the agent's shell tools have no token file to find. The process environment of the Copilot process itself still contains the token. That is unavoidable, and it is documented.

**5. Baked config: `/project-sandbox-config/copilot/config.json`.**
Render a minimal `config.json` with `trusted_folders: ["/workspace"]`, plus any other keys that task 1.2 confirms are needed to suppress first-run prompts.

*Task 1.2 result (2026-11-02):* The key is `trusted_folders` (snake_case), per "Configuring GitHub Copilot CLI" (https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli) and the config-dir reference (https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference). Newer releases move user-editable settings to `settings.json`, but trusted folders stay in the CLI-managed `config.json`. No other keys are documented for suppressing first-run prompts, so the e2e `trusted-folder` check settles that. The pin is `@github/copilot@1.0.98`, the latest version the npm registry listing showed in a web search. The registry itself was unreachable from this environment, so `scripts/update-pins.py` should re-check it. The `--effort`, `--no-ask-user`, `--disable-builtin-mcps`, and `--no-auto-update` flags come from the CLI command reference cited in Context. They could not be independently re-fetched here, so the e2e `headless-ok` and `isolation` checks are the confirmation. Mount it read-only. Provisioning copies it into a writable `COPILOT_HOME=/home/agent/.copilot`, because Copilot writes session state and logs there. Runtime env: `COPILOT_HOME`, `COPILOT_AUTO_UPDATE=false`, and `COPILOT_GH_HOST` for tenants only.

**6. Dispatch.**
- Interactive: `exec copilot --allow-all --no-auto-update --disable-builtin-mcps "$@"`. This matches codex's sandbox posture, where the container is the boundary.
- Headless: `exec copilot -p "$PROMPT" --allow-all --no-ask-user --output-format json --no-auto-update --disable-builtin-mcps "$@"`.
- `--model M` and `--effort E` are injected as two separate flags, the codex shape. `--fast` is rejected, since Copilot isn't added to `FAST_MODE_AGENTS`.
- Built-in MCPs are disabled because the GitHub MCP server would use the same token to act on GitHub. Users who want it can add it later on purpose (TODO).

**7. Transcripts and failure classification, without known schema.**
`transcript.py` gets Copilot detection and `render_copilot_markdown`. The JSONL schema isn't documented, so the renderer is defensive. It renders known record kinds: assistant text, tool calls and results, and errors. Unknown records become a compact fenced JSON line, and it never raises. A headless run that exits 0 is reclassified as 1 only when the stream's final record is clearly an error (or 75 when that record identifies a rate limit), following the Pi requirement. When the stream is ambiguous, the original exit code stands. The e2e suite captures real streams with `--capture-dir`, so later fixtures and tighter rules come from real data rather than guesses.

**8. Opt-in e2e suite: `scripts/e2e-copilot.py`.**
It is registered in `run-e2e-tests.sh` as the opt-in, billable suite `copilot` (`--with-copilot`, or `--only copilot`, and included in `--all-tests`). It uses the real token resolution, so a `pass` PAT or `gh` login is a prerequisite. Checks:

| Check | Account needed | Verifies |
|---|---|---|
| `headless-ok` | personal | a trivial prompt exits 0, the JSONL parses, the transcript renders |
| `headless-fail` | personal | a forced failure (invalid `--model`) yields a nonzero CLI exit; records the raw exit code and the final record |
| `trusted-folder` | personal | no trust prompt or trust error in a `-p` run without `COPILOT_ALLOW_ALL`; `config.json` still lists `/workspace` under `trusted_folders` |
| `isolation` | personal | the Copilot API is reachable, an unrelated public host is blocked, a bash session has no `/project-sandbox-secrets/copilot` and no `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`, built-in MCPs are disabled |
| `tenant` | GHE.com tenant (`--copilot-host`) | token resolution for the tenant entry, the pinned tenant hosts are enough for `headless-ok`, `copilot-api.<tenant>` is reachable |

`--capture-dir DIR` saves the JSONL and exit codes, with the token redacted. Output tails redact the token. Unit tests (`test_e2e_copilot.py`) cover only argument parsing, check selection, and redaction; they never invoke a container or Copilot.

## Risks / Trade-offs

- **[Risk] `gh` fallback scope.** The agent holds a token that may allow `git push` or workflow edits while GitHub is reachable. → Mitigation: the PAT is preferred, a warning prints on every fallback, built-in MCPs are off, the token isn't in bash sessions, and security.md documents it.
- **[Risk] Token visible in the Copilot process environment.** Tools Copilot runs inherit it. → Mitigation: this is inherent to how Copilot reads tokens. It is documented, and the PAT path limits what the token can do. Keychain staging is in TODO. Note that a plaintext `$COPILOT_HOME` token file would not improve on this.
- **[Risk] Undocumented JSONL and exit behavior.** → Mitigation: the renderer is defensive, the reclassification is conservative, and the e2e captures drive any follow-up.
- **[Risk] Tenant host set may be incomplete.** → Mitigation: check against GitHub's allowlist reference (task 1.1), confirm with the `tenant` e2e check, and keep `--extra-domain` as the escape hatch.
- **[Trade-off] Removing the heuristic breaks existing `--agent bash` Copilot usage.** Accepted: the explicit agent replaces it, and CHANGELOG plus usage docs show the migration.
- **[Trade-off] No devcontainer support.** Accepted for this change. Recorded in TODO.

## Migration Plan

Users of `--agent bash --prompt-text 'copilot …'` switch to `--agent copilot --prompt-text '…'`. Rollback means reverting the change. No persisted state is migrated. The staged token file is ephemeral.

## Open Questions

- The exact GHE.com tenant hostnames (task 1.1), and whether tenants also need `github.com`, `api.github.com`, or `*.githubcopilot.com` hosts. Resolved by doc review and the e2e `tenant` check.
- Whether `config.json` needs keys besides `trustedFolders` to avoid first-run prompts (for example a banner or a terms prompt). Resolved by the e2e `trusted-folder` check.
- Whether a telemetry opt-out exists. None is documented. `copilot-telemetry.githubusercontent.com` stays in the existing `--allow-github` set.
