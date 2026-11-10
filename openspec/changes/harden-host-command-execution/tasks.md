# Tasks

## 1. Research

- [x] 1.1 From jj's config reference and changelog: find the first release that stops reading `.jj/repo/config.toml`, and check whether jj 0.46 trusts or migrates such a file when it is planted in a repository. List exec-capable settings that the host commands (`workspace add/forget`, `git init`, `commit`, `status`, `describe`, `bookmark *`, `log`) can trigger. Record the results in design.md Decisions 3 and 4, and extend the override list if needed. Verify with a fixture repository whose config sets a signing program writing a marker: host `jj describe` with the overrides writes no marker.

## 2. Resolver and neutral directory

- [x] 2.1 Add `host_tools.resolve`/`run`, and switch `oauth_refresh`, `copilot_auth`, `agent_proxy.resolve_key`, `config_agents` (`security`), `git_identity` and `doctor` to them. Verify with tests on absolute argv[0] and neutral cwd per call site, plus the fake-`claude` cwd test from the "Token refresh in a project with hostile settings" scenario.
- [x] 2.2 Document the host execution model in `docs/security.md`. Verify the text against the call-site tests.

## 3. Host jj

- [x] 3.1 Add the jj overrides to every `_jj` call, and resolve `jj` through `host_tools`. Verify with argv tests for every host jj command.
- [x] 3.2 Add the `doctor` jj check from Decision 4 (task 1.1 found no version threshold): report the version, and warn on a `.jj/repo/config.toml` without `config-id`. Verify with tests for the warned and the migrated state, and that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
