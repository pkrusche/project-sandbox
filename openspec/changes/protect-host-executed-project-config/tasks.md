# Tasks

## 1. Read-only Git directories

- [ ] 1.1 Add read-only binds of `.git/hooks/` and `.git/info/` (created if missing) after the workspace and Git-backend mounts, for sessions without `--branch` and for colocated jj `--branch` sessions. Verify with argv tests for both modes on each runtime, including mount order.
- [ ] 1.2 Add an opt-in container test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`). It checks that writing `.git/hooks/pre-commit` and `.git/info/attributes` fails, and that an existing `pre-commit` hook still runs on an in-sandbox `git commit`. Verify on Docker and Apple `container`.

## 2. Git config baseline and restore

- [ ] 2.1 Implement the shared baseline with flock holders (design Decision 2). Verify with tests for:
  - a single session;
  - overlapping sessions where B starts after A's agent changed the config (B restores the original);
  - a `SIGKILL`ed holder (released);
  - baseline deletion after the last holder.
- [ ] 2.2 Implement no-follow quarantine and restore (design Decision 3), and call it first in `cli.main`'s `finally` block. Verify with tests for:
  - a modified file;
  - a file replaced by a symlink (target untouched);
  - a `config.worktree` created during the session (moved aside);
  - restoration on the interrupt and timeout paths.
- [ ] 2.3 Parse quarantined configs with `git config --file … --list --null` through `host_tools`, and flag the exec-capable keys from design Decision 4. Verify with table tests covering each key family.

## 3. Watched-path report

- [ ] 3.1 Implement no-follow manifests with the enumeration cap, and the diff kinds (added, removed, modified, changed type). Watch the `core.hooksPath` directory taken from the baseline. Verify with tests for each scenario in the "Changes to host-executed project config are reported" requirement.
- [ ] 3.2 Print the report after the egress-denial summary, and add `config_changes` to the JSON summary without changing existing fields. For `--branch` sessions, watch the workspace and protect the main `.git`. Verify with CLI tests for a session without `--branch`, a `--branch` session, no changes (no output), and a summary consumed the way project-sandbox-tui reads it (`pid`, `exit_code`, `session_id` unchanged).

## 4. Docs

- [ ] 4.1 Document the protections, the quarantine files, the report, the `config_changes` field, the "no host `git` during a live session" guidance and the first-run review suggestion in `docs/security.md` and `docs/usage.md`. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
