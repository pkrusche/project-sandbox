# TODO - outstanding items

- The macOS shared-filesystem metadata cache window is handled for git worktrees
  only (`worktree.METADATA_CACHE_WINDOW`, applied in `cli._wait_for_worktree_metadata`).
  jj workspaces mount `.jj/repo` and the main repo's git backend the same way and
  may have the same exposure when a workspace directory is deleted and recreated
  within ~1s; `scripts/e2e-jj-workflow.sh` passes today, so it is unconfirmed and
  untreated.
