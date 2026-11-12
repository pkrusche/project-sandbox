# Tasks

## 1. Branch sessions

- [ ] 1.1 Pin the writer formats (design Context) with fixture tests: create a colocated jj repository with a non-colocated workspace and a Git worktree, and assert the pointer, `git_target`, gitfile, `commondir` and back-reference contents. Verify the tests pass on the host jj and Git.
- [ ] 1.2 Rewrite `repo_store_mount` and `git_backend_mount` to compute sources and targets (design Decision 1) and compare the pointer files, refusing mismatches with the file, expected value and recovery command. Verify with tests that the planted `/usr/local/bin` pointer and a planted absolute `git_target` are refused, that relative and absolute spellings of the correct values are accepted, and with argv tests that an unchanged layout yields the same mounts as before.

## 2. In-place sessions

- [ ] 2.1 Add the structure, back-reference, protected-location and target checks to `vcs_access.inspect` (design Decision 2). Verify with tests for a gitfile naming `~/.ssh`, a `commondir` leaving the repository, a jj pointer to a non-store directory, a target under `/usr`, and a legitimate linked worktree of another repository.

## 3. Docs and integration

- [ ] 3.1 Describe the rules and the refusal messages in `docs/vcs-access.md` and `docs/security.md`. Verify the documented recovery commands on a fixture, and that `uv run pytest -q`, `uv run ruff check src scripts tests` and `uv run python -m compileall -q src tests` pass.
- [ ] 3.2 Extend the security e2e suite with a jj `--branch` case that plants both pointers in one session and asserts the next launch is refused. Verify it passes on Docker and Apple `container`.

## Workflow follow-up

- Archive with `/opsx:archive`, creating `openspec/specs/vcs-metadata-mounts/`.
