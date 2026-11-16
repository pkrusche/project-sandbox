# TODO - outstanding items for next release

- `docs/runtime.md` step 2 of "End-to-End Flow" (line 11) says agent
  credentials are staged "under `/tmp`". That step is the render-only flow,
  whose persistent devcontainer staging really is
  `/tmp/project-sandbox-<uid>/<digest>/<agent>` (`config_agents.py`), so the
  location is right but vague. Name the exact path, and add that CLI runs stage
  into [private session inputs](security.md#private-session-inputs) instead
  (the `--agent` flow's step 2 already says so). Found while checking Colima
  mount visibility; not part of `add-colima-runtime`.
- Live markdown view for headless jobs seems broken, I now see only JSON lines 

## Decided, ready to implement (2026-11-16)

- **Headless prompts that start with `-`.** The entrypoint passes the prompt as
  one argv element (`codex exec`, `opencode run` positionally; Claude, Pi and
  Copilot via `-p "$PROMPT"`), so a prompt beginning with `-` can be parsed as a
  flag. Decision: on the host, before staging the prompt file, prepend a single
  space when the prompt starts with `-`. The text reaches the model unchanged
  in meaning, and no CLI parser treats it as an option, so no reliance on
  per-parser `--` handling. Don't trim: prompts like a Markdown list
  (`- fix X`) or `--dry-run is broken` would lose meaning. Unit-test in
  `tests/test_cli.py`; add an e2e smoke test with a `--help` prompt. The
  subcommand case (`codex exec resume`) only applies to a prompt that is
  exactly a subcommand name, since the prompt is a single argument.
- **Colima workspace roots: document, don't extend the deny list.**
  `colima_profile.CREDENTIAL_DIRS` stays as is. In the Colima docs, recommend a
  dedicated workspace directory and name roots to avoid because they would be
  exported read-write: `~/Library` (Keychains, browser cookies), `~/.colima` /
  `~/.lima` (the VM's own config and SSH keys), `~/.kube`, and `~/.config`.
- **Disable fsmonitor in host-side Git calls.** Pass `-c core.fsmonitor=false`
  in `worktree._git()`, which every `worktree.finalize` call goes through. With
  `--vcs-access full` the agent can set `core.fsmonitor` in `.git/config`, and
  the host's `git status`/`add`/`commit` would execute it. No user-visible cost.
  Add a regression test that asserts the argv. Hooks, filters and signing stay
  in ROADMAP.md ("Host capture can still execute workspace-controlled code").

## Deferred

- **Devcontainer hidden-VCS masks are created in the system temp dir.**
  `devcontainer.render` calls `vcs_access.new_mask_dir()` (a `mkdtemp` under
  `/tmp`) and writes that path into the persistent `devcontainer.json`. Temp
  cleaners or a reboot delete it, and each regeneration leaks the old
  directory. Left for the larger devcontainer rework. Notes for then: the mask
  contents are static, so a deterministic per-project directory under the state
  root, recreated by `initializeCommand`, would fix both the leak and the reboot
  problem; check whether Colima-backed devcontainers (home-only exports) can see
  `/tmp` masks at all today.
