# Design

## Context

- **Sessions without `--branch`:** the project (including `.git/`) is bind-mounted read-write at `/workspace`. No part of `.git` is protected today.
- **Colocated jj `--branch` sessions:** the main repository's `.git` is bind-mounted read-write at a computed target (see `jj-only-branch-workspaces`). jj in the container writes objects and refs there.
- **Apple `container`:** rejects single-file bind mounts ("is not a directory"), but nested directory binds work. The existing `/workspace/.project-sandbox` mask is one.
- **Session teardown:** happens in `cli.main`'s `finally` block (`cli.py:1200–1245`): egress cleanup, record finish, forwarders, staged secrets, workspace finalization, then the JSON summary (`_write_json_summary_unguarded`, `cli.py:1284`). The summary currently carries `session_id`, `container_name`, `workspace_path`, `agent`, `bookmark`, `jj_change_id`, `exit_code` and the timestamps.

## Goals / Non-Goals

**Goals:**
- An agent can't leave behind Git hooks or Git config that the user's host `git` will execute.
- The user learns about every change to agent and IDE config that their host tools may run automatically.

**Non-Goals:**
- Preventing agents from editing project files, including the watched ones.
- Protecting against the user's host `git` *during* a running session in the same repository (see Risks).
- Inspecting committed content (`Makefile`, build scripts).

## Decisions

### 1. Directories read-only; config files restored afterwards

| Path | Mechanism | Why |
|---|---|---|
| `.git/hooks/`, `.git/info/` | read-only bind of the same host directory over itself | Directories work on every runtime. Existing hooks still run in the sandbox. Agents rarely need to write there (`git sparse-checkout` is the exception, and it fails with a clear error). |
| `.git/config`, `.git/worktrees/*/config.worktree` | snapshot before the session, then quarantine and restore after it | Single files can't be bind-mounted on Apple. A read-only bind on Docker/Podman alone would make `git config`, `remote add` and `branch -u` fail on some runtimes and not others. Restoring behaves the same everywhere, and keeps in-session Git usable. |
| Watched agent/IDE paths | manifests before and after, then a report | These are legitimately edited and committed, so blocking or reverting would break normal work. |

The mounts are added after the workspace and Git-backend mounts, so they nest on top. A missing `hooks/` or `info/` is created (empty, `0755`) through a no-follow `mkdir` under `.git`, because Apple rejects a bind onto a missing target.

### 2. A shared baseline for concurrent sessions

A per-session snapshot is wrong when sessions overlap: a session that starts while another agent is running would capture that agent's edits as its "known good" state. Instead, each repository has one baseline:

```
$XDG_STATE_HOME/project-sandbox/git-baseline/<sha256(realpath .git)>/
  config, worktrees/<name>/config.worktree    copies taken by the first session
  holders/<session-id>                         flock held by each live session
```

- **Start**, under `path_lock("git-baseline", <.git>)`: if any holder lock is still held, reuse the baseline. Otherwise, delete stale holders and take a fresh baseline from the current files. Then register this session's holder.
- **End**, under the same lock: restore every file that differs from the baseline, then release this session's holder. When no holders remain, delete the baseline.

Holders are flocks, like the session leases in `private-session-state`, so a killed CLI frees its holder automatically.

Once `private-session-state` lands, the baseline moves under `projects/<key>/`.

### 3. Restoring without following links

For each file that differs from the baseline:
1. Open `.git` as a directory descriptor (no-follow).
2. `renameat(name → name.project-sandbox-<session-id>)`, which moves whatever is there, symlink included, without following it.
3. Write the baseline through a no-follow temporary file and `renameat` it into place.

Before `private-session-state`'s `PrivateDir` exists, the same steps use `os.open(..., dir_fd=…, O_NOFOLLOW)`, `os.rename(src_dir_fd=…, dst_dir_fd=…)` and `os.replace` directly.

The restore runs first in the `finally` block, before workspace finalization, the summary and any host `jj` command. It also runs on interrupts and timeouts, because those paths pass through the same `finally`.

### 4. Flagging exec-capable keys

The report parses the agent's config version with `git config --file <quarantine> --list --null` on the host. Parsing a file Git is pointed at with `--file` doesn't execute anything. That call runs through `host_tools` from a neutral directory (see `jj-only-branch-workspaces`).

Keys are flagged when they match:
- `core.hooksPath`, `core.fsmonitor`, `core.sshCommand`, `core.pager`, `core.editor`, `core.askPass`, `core.gitProxy`, `core.attributesFile`;
- `filter.*`, `diff.*.textconv`, `diff.*.command`, `diff.external`, `merge.*.driver`;
- `gpg.program`, `gpg.*.program`, `credential.helper`, `credential.*.helper`;
- `alias.*` values starting with `!`;
- `include.path`, `includeIf.*.path`;
- `sequence.editor`, `pager.*`, `interactive.diffFilter`;
- `remote.*.uploadpack`, `remote.*.receivepack`, `remote.*.vcs`, `url.*.insteadOf`;
- `protocol.allow`, `protocol.*.allow`, `uploadpack.packObjectsHook`;
- `init.templateDir`, `*.cmd` (difftool, mergetool, man, browser), `web.browser`.

The list is a reporting aid, not a filter: every change is reverted regardless.

### 5. Manifests and report

- **Manifest:** `{relative path: (type, size, sha256)}`, built with `os.scandir(follow_symlinks=False)`. Symlinks are recorded as links with the hash of their target string, never followed. Recursion is capped at 2,000 entries per watched directory. Beyond that, the report says "changed (not enumerated)".
- **`core.hooksPath` directory:** read from the baseline config. It is watched only when it resolves inside the project or workspace.
- **Printing:** the report is printed after the egress-denial summary, so headless sessions show it after the transcript. It is also written as `config_changes: [{path, tree: "workspace"|"git", kind, quarantine?, exec_keys?}]` in the summary.

## Risks / Trade-offs

- [The user's host `git` runs while a session is active in the same repository] → It honors any config the agent wrote since the session started, until the restore. The docs say not to run `git` in a repository with a live session. `jj` is unaffected.
- [Legitimate in-session config changes are lost, for example `git remote add`] → They're kept in the quarantine file and named in the report. Re-applying is one `git config` command, or copying the file back after review.
- [Agents can't install hooks, such as `pre-commit install`] → Intended. The error comes from Git inside the sandbox, and the docs explain why.
- [Report noise from IDE files the user edits on the host during a session] → It's a report only. Tools that touch `.vscode/settings.json` frequently may show up. Acceptable.
- [Large watched directories] → Capped enumeration (Decision 5).

## Migration Plan

There's nothing to migrate. On the first session after upgrade, the baseline is the current `.git/config`, which may already contain changes from earlier sessions. The docs suggest running `git config --list --show-origin` once, to review it.
