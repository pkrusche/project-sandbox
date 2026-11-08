## Why

Users with a GitHub Copilot subscription (personal, Copilot Business, or Copilot Enterprise on GitHub Enterprise Cloud, including GHE.com data-residency tenants) have no first-class way to run the GitHub Copilot CLI (`@github/copilot`, binary `copilot`) inside project-sandbox. Today the only path is `--agent bash` with a prompt whose first word is `copilot`, which a heuristic (`_uses_github_copilot_cli`) detects to switch on `--allow-github`. That path installs nothing, forwards no token, cannot target a GHE.com tenant, and widens the firewall based on prompt text. Pi and OpenCode already cover gateway/BYOK use; the value of this change is running agents on a **Copilot subscription**.

## What Changes

- Add `"copilot"` as a supported `--agent` value with interactive and headless dispatch. Headless runs use `copilot -p "$PROMPT" --allow-all --no-ask-user --output-format json --no-auto-update --disable-builtin-mcps`, plus `--model`/`--effort` when given.
- Install `@github/copilot` in the image, pinned, behind a new `install_copilot` Dockerfile flag.
- Add `--copilot-host HOST`, which accepts `github.com` (the default; covers personal, Business, and GHEC accounts) or `<tenant>.ghe.com` (GHEC with data residency). Other hosts, including self-hosted GHES, are rejected.
- Resolve the Copilot token on the host at every launch, in this order:
  1. a fine-grained PAT (only the "Copilot Requests" permission) from `pass show <entry>`. The default entry is `copilot-github-token` for github.com and `copilot-github-token/<host>` for a tenant; `--copilot-token-pass ENTRY` overrides it.
  2. a fallback to `gh auth token --hostname <host>`, which prints a token-scope warning on every run.
  3. a clear error naming both remedies if neither works.

  The token is staged as a 0600 file, mounted read-only, and exported only inside the `copilot` entrypoint arms. `--dry-run` does no lookup.
- `--agent copilot` implies `--allow-github`. For a GHE.com tenant, the firewall also pins that tenant's Copilot and API hosts. **BREAKING:** remove the `--agent bash` prompt heuristic (`_uses_github_copilot_cli`). Bash sessions no longer open GitHub because of prompt text, and they never receive a Copilot token.
- Reject `--agent copilot` combined with `--agent-proxy`. Also reject `--fast` for Copilot.
- Seed `$COPILOT_HOME/config.json` with `/workspace` as a trusted folder, and set `COPILOT_HOME`, `COPILOT_AUTO_UPDATE=false`, and `COPILOT_GH_HOST` (tenants only).
- Add a Copilot JSONL transcript renderer, and reclassify failed headless runs as nonzero exits whenever the event stream shows a failure.
- Add an opt-in, billable e2e suite (`copilot`) that captures real JSONL and exit codes, checks trusted-folder seeding, audits network and token isolation, and optionally exercises a GHE.com tenant.
- Docs: usage and security notes for Copilot. TODO.md entries for deferred work: Keychain token staging, devcontainer support, and GHES.

## Capabilities

### New Capabilities
- `copilot-agent-support`: running the GitHub Copilot CLI as a selectable agent on a Copilot subscription. Covers host selection, token resolution and isolation, firewall scope, image install, dispatch, transcripts, and opt-in e2e verification.

### Modified Capabilities
(none. The bash-heuristic removal is recorded as a requirement of the new capability, because no existing spec describes the heuristic.)

## Impact

- **Code**: `cli.py` (agent list, flags, validation, removal of the heuristic, token resolution, warnings), a new `copilot_auth.py` (pass/gh resolution and host validation), `config_agents.py`, `container_cli.py`, `dockerfile.py` + `templates/Dockerfile.j2`, `templates/entrypoint.sh.j2`, `templates/_provision.sh.j2`, `firewall.py` + `templates/init-firewall.sh.j2`, `transcript.py`, `scripts/update-pins.py`.
- **Not touched**: `devcontainer.py` (Copilot is not offered in devcontainers in this change), `oauth_refresh.py`, `token_expiry.py` (Copilot is a safe no-op there).
- **Tests**: `test_cli.py`, `test_container_cli.py`, `test_renderers.py`, `test_transcript.py`, `test_oauth_refresh.py`/`test_token_expiry.py` (no-op regression), a new `test_copilot_auth.py`, and `test_e2e_copilot.py` (script argument and selection logic only).
- **E2E**: a new `scripts/e2e-copilot.py`, registered as the opt-in `copilot` suite in `scripts/run-e2e-tests.sh`.
- **Docs**: `docs/usage.md`, `docs/security.md`, `docs/development.md`, `CHANGELOG.md`, `TODO.md`, and a one-line mention in `README.md`.
- **Security-sensitive**: a new secret mount (`/project-sandbox-secrets/copilot`). The `gh` fallback gives the agent a token that is often broadly scoped while GitHub is reachable. Firewall widening is now driven by the agent choice instead of prompt text. There are new host-derived allowlist entries for GHE.com tenants.
