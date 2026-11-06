# Tasks

## 1. Confirm external facts

- [x] 1.1 Check the GHE.com tenant hostnames that Copilot CLI needs (`<tenant>.ghe.com`, `api.`, `copilot-api.`, `copilot-telemetry-service.`, and any `*.githubcopilot.com` / `github.com` dependency) against GitHub's published Copilot allowlist reference for data residency. Verify by recording the confirmed set and its source URL in design.md Decision 2.
- [x] 1.2 Pick the current published `@github/copilot` version and confirm from GitHub's docs the `config.json` trusted-folders key name and the `--effort`, `--no-ask-user`, `--disable-builtin-mcps`, and `--no-auto-update` flags. Verify by recording the version and key name in design.md Decision 5.

## 2. Agent registration and image install

- [x] 2.1 Add `"copilot"` to `SUPPORTED_AGENTS` (not to `FAST_MODE_AGENTS`) and to `_CONFIGURED_AGENTS`. Reject `--agent copilot` with `--agent-proxy` or `--fast` before any lookup or write. Verify with `test_cli.py` cases for acceptance and both rejections.
- [x] 2.2 Add an `install_copilot` flag to `dockerfile.py::render()` and an `npm install -g @github/copilot@<pin>` block to `Dockerfile.j2`. Register the pin in `scripts/update-pins.py`. Verify with a `test_renderers.py` install-line assertion and a `test_update_pins.py` case.
- [x] 2.3 Add `--model`/`--effort` help examples for Copilot and inject them as two separate flags for headless Copilot. Verify with `test_cli.py` model, effort, and combined injection tests.

## 3. Host selection and token resolution

- [x] 3.1 Add `--copilot-host` (default `github.com`) and its validation, accepting `github.com` or `<tenant>.ghe.com` and rejecting GHES-style hosts with a supported-forms message. Verify with `test_copilot_auth.py` and `test_cli.py` cases for valid, uppercase/whitespace, multi-label, and foreign hosts.
- [x] 3.2 Implement `copilot_auth.resolve_token()`: the pass default/explicit entry, the `gh auth token --hostname` fallback, the error naming both remedies, `ghp_` rejection, and timeouts. Verify with `test_copilot_auth.py` using a mocked `subprocess`, covering every source/failure combination and asserting `gh` is never called when pass succeeds or when an explicit entry fails.
- [x] 3.3 Wire resolution into `cli.py` after validation: add `--copilot-token-pass`, print the `[W]` token-scope warning only on the `gh` source, add the token to secret redaction, and skip everything under `--dry-run`. Verify with `test_cli.py` tests asserting the warning text, redacted verbose output, and that dry-run invokes neither `pass` nor `gh` and writes no files.
- [x] 3.4 Document the pass-PAT setup (fine-grained PAT with only "Copilot Requests", default entry names, tenant entries, `gh` fallback) in `docs/usage.md`, and the token-scope trade-off in `docs/security.md`. Verify that the documented commands match the CLI help and that the dry-run examples run as written.

## 4. Token staging, mounts, and container environment

- [x] 4.1 Stage the token to `credentials_dir(..., "copilot")/token` (file 0600, directory 0700), rewritten each launch and removed best-effort at session end. Verify with a `test_cli.py`/`test_session.py` test of file mode, rewrite, and cleanup, plus a scan confirming no token appears under `.project-sandbox/`.
- [x] 4.2 In `container_cli.build_mount_specs()`/`build_run_argv()`, mount the token directory read-only at `/project-sandbox-secrets/copilot` only for `run_agent == "copilot"`, and mount the baked config at `/project-sandbox-config/copilot`. Set `COPILOT_HOME`, `COPILOT_AUTO_UPDATE=false`, and `COPILOT_GH_HOST` (tenant only). Verify with `test_container_cli.py` cases for copilot (both host kinds), bash (with and without credential forwarding), and each other agent, asserting the mount and env are present or absent.
- [x] 4.3 Render `copilot/config.json` with `/workspace` trusted. Copy it into writable `$COPILOT_HOME` in `_provision.sh.j2`, never copying the token there. Add the `.gitignore` allowlist entries for the generated config. Verify with `test_renderers.py` for the config content and provision script, and with `test_gitignore.py`.
- [x] 4.4 Add the `copilot)` and `copilot-headless)` arms to `entrypoint.sh.j2`, exporting `COPILOT_GITHUB_TOKEN` from the mounted file only for the `exec`, with the interactive and headless argv from design Decision 6. Verify with `test_renderers.py` and `test_cli.py` headless-dispatch tests mirroring the Pi ones, and assert the bash arm never reads the secret path.

## 5. Firewall scope and heuristic removal

- [x] 5.1 Delete `_uses_github_copilot_cli` and make `_allow_github()` true for `run_agent == "copilot"` while keeping proxy-mode behaviour. Replace the heuristic tests with ones asserting that `--agent bash --prompt-text 'copilot …'` no longer enables GitHub and that `--agent copilot` does. Verify with `test_cli.py`.
- [x] 5.2 Add `copilot_tenant_hosts` to `firewall.render()` and `init-firewall.sh.j2` (sandbox script only), using the set confirmed in 1.1 and DNS-pinned like the existing GitHub hosts. Verify with `test_renderers.py`/`test_proxy_firewall.py` that the hosts appear only for a tenant host and are absent from the devcontainer script.
- [x] 5.3 Update `docs/security.md` (Copilot implies `--allow-github`, tenant hosts, built-in MCPs disabled, the token in the Copilot process env) and add a CHANGELOG entry for the breaking heuristic removal with migration guidance. Verify the docs text against the rendered firewall from 5.2.

## 6. Transcripts and failure classification

- [x] 6.1 Add Copilot JSONL detection and a defensive `render_copilot_markdown` to `transcript.py`, plus the live-view translation; unknown records become raw JSON. Verify with `test_transcript.py` using synthetic fixtures (assistant text, tool call/result, error, unknown kind, malformed line).
- [x] 6.2 Reclassify an exit-0 headless Copilot run to 1 (or 75 on a rate limit) only when the final record is unambiguously an error. Verify with `test_cli.py`/`test_session.py` cases for error-final, recovered error, ambiguous stream, and nonzero passthrough.
- [x] 6.3 Add `"copilot"` no-op regression tests for `oauth_refresh.refresh_host_token` and `token_expiry` without changing either module. Verify with `test_oauth_refresh.py` and `test_token_expiry.py`.

## 7. Opt-in end-to-end suite

- [x] 7.1 Write `scripts/e2e-copilot.py` with the `headless-ok`, `headless-fail`, `trusted-folder`, `isolation`, and `tenant` checks, plus `--copilot-host`, `--capture-dir`, `--runtime`, `--base-image`, `--output-tail`, token redaction, and skip reporting for `tenant` when no tenant is given. Verify with `test_e2e_copilot.py` covering argument parsing, check selection, and redaction, with no container or Copilot invocation.
- [x] 7.2 Register `copilot` as an opt-in billable suite in `scripts/run-e2e-tests.sh` (`--with-copilot`, `--only copilot`, included in `--all-tests`, listed in `--list`). Verify that `--list` shows it and that a default run skips it without any token lookup.
- [x] 7.3 Document the suite in `docs/development.md`: prerequisites (pass PAT or `gh` login), billing, the personal and tenant invocations, and how captures feed transcript fixtures. Verify the documented commands match the script's `--help`.

## 8. Integration and wrap-up

- [x] 8.1 Run `uv run project-sandbox --agent copilot --dry-run …` for github.com and a tenant host. Verify the preview shows the token source, mounts, env, and firewall additions, and that no `pass`/`gh` call, file write, or container start happens.
- [x] 8.2 Add a one-line Copilot mention to `README.md`, and add TODO.md entries for Keychain token staging, devcontainer support, GHES, opt-in GitHub MCP, and tightening the transcript/exit rules from e2e captures. Verify that the files list each item.
- [x] 8.3 Run `uv run python -m compileall src tests`, `uv run pytest -q`, and `~/.npm-packages/bin/openspec validate add-copilot-agent-support --strict`, and record the results. Note that the `copilot` e2e suite was not run in this environment.
  - Results (2026-11-02): `uv run python -m compileall src tests scripts` OK; `uv run pytest -q` 961 passed, 4 skipped; `uv run ruff format --check .`, `uv run ruff check .`, and `uv run ty check` clean; `openspec validate add-copilot-agent-support --strict` reports the change is valid. `~/.npm-packages/bin/openspec` does not exist here, so the `openspec` on PATH was used.
  - The `copilot` e2e suite was **not** run in this environment: there is no container runtime, no Copilot token, and no network access to GitHub or npm. The JSONL schema, exit codes, trusted-folder seeding, the four CLI flags, the tenant host set, and the `@github/copilot@1.0.98` pin remain unconfirmed until it is run.

## Workflow follow-up

- Run the `copilot` e2e suite with a personal account (and a GHE.com tenant if available) using `--capture-dir`, then fold real JSONL into `tests/fixtures` and tighten 6.1/6.2 if needed.
- Archive the change after review.
