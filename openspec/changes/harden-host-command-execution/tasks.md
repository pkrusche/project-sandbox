# Tasks

## 1. Resolver and neutral directory

- [ ] 1.1 Add `host_tools.resolve`, `run` and `launch_scope` (design Decisions 1–2), porting the `sec-fixing-claude` branch's `host_tools.py`, and switch `oauth_refresh`, `copilot_auth`, `agent_proxy.resolve_key`, `config_agents` (`security`), `git_identity`, `doctor` and runtime selection to them. Verify with tests on absolute argv[0] and neutral cwd per call site, a relative-`PATH` and a project-`PATH` test, and the fake-`claude` hostile-settings test.

## 2. Host Git and jj

- [ ] 2.1 Route every `worktree._git` call through `host_tools.git` with the overrides, and add `--no-verify` to the capture commit (Decision 3). Verify with a fixture repository whose `.git/hooks/pre-commit`, `core.hooksPath` and `core.fsmonitor` write markers: setup and finalize of a Git `--branch` session create no marker, and argv tests show the overrides on every call.
- [ ] 2.2 Add the jj overrides to every `_jj` call and resolve `jj` through `host_tools` (Decision 4). Verify with argv tests for every host jj command and a marker test for a signing program and `git.executable-path` in repository config.

## 3. doctor and docs

- [ ] 3.1 Add the `doctor` jj check (Decision 5). Verify with tests for the warned and the migrated state and the version report.
- [ ] 3.2 Document the host execution model, the snapshot-commit hook policy and the remaining filter risk in `docs/security.md`, and the jj check in the `doctor` paragraph of `docs/usage.md`. Verify the text against the call-site tests, and that `uv run pytest -q`, `uv run ruff check src scripts tests` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- Land before `isolated-vcs-access`, which uses `host_tools.git`/`jj` for its setup and capture.
- Archive with `/opsx:archive`, creating `openspec/specs/host-command-execution/`.
