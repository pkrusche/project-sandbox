# TODO - outstanding items

## Firewall: verify multi-resolver rules on a real iptables host

Code is complete and the render path is covered by
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`.
The unit tests are render-only by policy and do not exercise live iptables.
Outstanding: run the rendered script on a host with iptables and multiple
`nameserver` entries in `resolv.conf`, then confirm allowlisted-domain
pre-resolution works across the resolver setup and post-firewall DNS egress does
not leak before treating this as shipped.

## Isolate concurrent subagents in separate clones, merge back on teardown

Every `--branch` jj agent shares one repo's `.jj/repo` store and — since we now
also mount the git backend — its `.git`, both bind-mounted read-write into each
container. That fits jj's concurrent-workspace model on a shared-kernel runtime,
but concurrent writes from *inside* multiple containers to a single shared store
are not obviously safe across separate VMs (Apple `container` + VirtioFS), where
lock-file and rename atomicity may not hold.

Plan: give each subagent its own clone, then merge/rebase the agent's bookmark 
back into the parent repo during teardown. This removes the shared-store race 
entirely and keeps each agent's blast radius isolated.

Note the git-worktree (`--branch` non-jj) path — which shares `.git` the same way —
should use the same approach.

Interim mitigation already in place: a host-side exclusive lock serializes
`jj_workspace.finalize()` (`_teardown_lock`), so concurrent teardowns can't
interleave their store mutations. It does not address concurrent in-container
writes; this item supersedes it.

## Code review findings (2026-07-01)

### Bugs

- **`--log` tilde paths validate but then crash mid-run.**
  `_validate_session_inputs` checks `Path(args.log).expanduser().resolve().parent`
  (`cli.py:943`), but `_build_session_command` opens `Path(args.log).resolve()`
  without `expanduser()` (`cli.py:1195`). `--log ~/x.log` passes validation
  against `$HOME`, then `session.run` fails with an unhandled
  `FileNotFoundError` on `$CWD/~/x.log` — after the worktree/branch has been
  created and the image built. Expand once, up front, and reuse the resolved
  path in both places.

- **Dead `copilot` branch in `_uses_github_copilot_cli`.**
  `cli.py:953` checks `run_agent == "copilot"`, but `copilot` is not in
  `SUPPORTED_AGENTS` and argparse `choices` makes the value unreachable.
  Either wire up a real copilot agent or delete the branch (the
  `--agent bash --prompt-text 'copilot …'` detection below it still works).

- **The `.project-sandbox/.gitignore` whitelist is dead code.**
  `_update_project_gitignore` adds `.project-sandbox/` to the project
  `.gitignore`, and git cannot re-include files under an excluded directory,
  so the `!claude/settings.json` / `!Dockerfile` / … negations written by
  `_write_project_sandbox_gitignore` (`cli.py:1395`) can never take effect.
  Also `history/` there is redundant under the leading `*`. Decide which
  mechanism owns ignore behavior and remove the other (or document that the
  nested file only matters for users who drop the outer ignore).

- **`dockerfile_checksum.record` drops baselines for other Dockerfiles.**
  `record()` rewrites the state file with only the currently tracked paths
  (`dockerfile_checksum.py:75`). Alternating runs between two `--dockerfile`
  values (or between `--dockerfile` and plain `base_image`) silently erases the
  other file's tamper baseline, so the next change to it goes unreported.
  Merge-update the recorded dict instead of replacing it.

- **Session log tee can die on non-UTF-8 agent output.**
  `session.run` uses `Popen(..., text=True)` with strict decoding; a single
  invalid byte in container output raises `UnicodeDecodeError` inside
  `_tee_output`'s daemon thread, silently truncating both the console stream
  and the log file while the session continues. Pass `errors="replace"`.

- **`--no-forward-credentials` still requires host agent config.**
  Its help promises "start unauthenticated and log in inside the sandbox", but
  `_ensure_agent_available` rejects `--agent claude` when `~/.claude` is
  missing, and `dockerfile.render(install_agents=available_agents)` only
  installs an agent CLI into the image when its host config dir exists. On a
  host without that agent configured, the advertised flow is impossible.

- **`--effort` help text is wrong about the default.** It says
  "(default: xhigh)" (`cli.py:193`), but the default is `None` — the
  entrypoint passes no flag and the agent's own default applies.

### Robustness / security hardening

- **`--api-key-env` secrets are visible in host process listings.** Values are
  interpolated into the `docker run --env NAME=VALUE` argv for the entire
  session. docker/podman support `--env NAME` (inherit from the client
  environment) and `--env-file`; use one of those so secrets never hit argv.
  (Docs already soft-warn about "runtime metadata"; argv exposure is broader.)

- **Inconsistent failure modes for bad user input.** Missing `project`,
  `--prompt`, or `--api-key-env-file` paths raise `FileNotFoundError`
  tracebacks from `resolve_strict`; raw/non-bind `--mount` values under
  `--runtime chroot` raise `ValueError` from `build_chroot_argv`; EOF at the
  Dockerfile-changed `input()` prompt raises `EOFError`. Other input errors
  are clean `SystemExit` messages. Normalize.

- **`--mount` conflict detection is a substring test.**
  `any(vcs_dir_str in m for m in extra_mounts)` (`cli.py:1158`) false-positives
  on e.g. a mount of `/repo/.git-backup` when the metadata mount is
  `/repo/.git`. Parse the mounts and compare source/target paths.

- **`--no-build` interactions.** `--no-build --force-build` is accepted and
  `--no-build` silently wins; `--no-build` with no existing image fails late
  with a raw runtime error at `container run`. Reject the combination and
  pre-check image existence for a clear message.

- **Devcontainer credential mounts break after `/tmp` cleanup.** The generated
  `devcontainer.json` binds staged credentials from
  `/tmp/project-sandbox-<uid>/…`; a reboot or tmp reaper removes them, and
  `initializeCommand` recreates only the history/mask dirs — "Reopen in
  Container" then fails until the CLI is re-run. Recreate (or at least
  pre-create empty) credential dirs in `initializeCommand`, or stage somewhere
  less volatile.

- **Verify `claude auth status` actually exists.** `oauth_refresh._AGENTS`
  delegates the pre-launch token refresh to `claude auth status` /
  `codex login status` with output captured and errors swallowed; if the
  subcommand doesn't exist in the pinned CLI version, the refresh feature
  silently never works. Add a one-time verbose diagnostic (or a test pin)
  so a broken delegate command is noticeable.

### Minor cleanups

- `cli.py:1251-1255`: the `create_prompt_files` if/else computes the identical
  `mask_source` in both branches — only the `print` differs.
- `_dry_run` re-runs `_validate_api_key_injection_args`, already done in
  `main()` before dispatch.
- `devcontainer.py:152`: `if HISTORY_HISTFILE:` guards a non-empty module
  constant — always true.
- `session.count_lines` docstring says "count newlines" but the
  implementation counts lines (a trailing unterminated line is included).
- `session.run` dry-run prints `cmd >  'log'` (double space) because
  `redirect` is `"> "` and `print` adds another separator.
- `jj_workspace._list_workspaces` distinguishes template vs. human output with
  a `":" not in line` heuristic, which misparses workspace paths containing a
  colon.
