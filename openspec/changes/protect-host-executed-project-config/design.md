# Design

## Context

- **Shared sessions** (`vcs-session-modes`): the project, including `.git/` and `.jj/`, is bind-mounted read-write at `/workspace`. Only one shared session runs per repository.
- **Isolated sessions:** the main repository's metadata is not mounted. The session directory's files come back as one commit.
- **Apple `container`:** rejects single-file bind mounts ("is not a directory"), but nested directory binds work.
- **Host jj** has `git.executable-path = "git"`, so `jj git push` and `fetch` run Git against `.git/config`.
- **Session teardown:** happens in `cli.main`'s `finally` block (`cli.py:1200–1245`), ending with the JSON summary (`_write_json_summary_unguarded`, `cli.py:1284`).

## Goals / Non-Goals

**Goals:**
- An agent in a shared session can't leave behind Git hooks, Git config or jj config pointers that the user's host tools will honor.
- Where the platform allows, it can't even change them while the session runs.
- The user learns about every change to config their host tools may run automatically, as it happens in shared mode and at the end in both modes.

**Non-Goals:**
- Preventing agents from editing project files, including the watched ones.
- Inspecting committed content (`Makefile`, build scripts).

## Decisions

### 1. Protection per path

| Path | Mechanism | Why |
|---|---|---|
| `.git/hooks/`, `.git/info/` | read-only bind of the same host directory over itself | Directories work on every runtime. Existing hooks still run in the sandbox. |
| `.git/config`, `.git/worktrees/*/config.worktree`, `.jj/repo/config-id`, `.jj/workspace-config-id` | hard read-only during the session (Decision 2), plus snapshot and restore afterwards (Decision 3) | These are single files that host tools read on every command. |
| `.jj/repo/config.toml` | moved aside after the session if it appeared or changed | Only older jj releases read it. `doctor` warns about those (`harden-host-command-execution`). |
| Watched agent/IDE paths | manifests, the watcher and a report | These are legitimately edited and committed, so blocking or reverting would break normal work. |

A missing `hooks/` or `info/` is created (empty, `0755`) through a no-follow `mkdir` under `.git`, because Apple rejects a bind onto a missing target.

### 2. Hard read-only for the config files

- **Linux (Docker, Podman):** each existing file is bind-mounted read-only over itself, after the workspace mount. The container sees the inode as it was at launch. A host-side atomic replace during the session isn't visible inside, which is harmless.
- **macOS (Apple `container`, Docker Desktop, Podman machine):** before launch, the host runs the equivalent of `chflags uchg` on each file. The flag is enforced by the host filesystem, so writes, renames and deletes arriving through the VM's file sharing fail. After the container exits, the host clears the flag on exactly the files it set, recorded in session state.
- **Stale flags:** a killed CLI leaves the flag set. The next launch for the repository, and `doctor`, clear flags recorded for sessions whose lease is free.
- **Fallback:** if the spike (task 1.1) shows a mechanism doesn't hold on a platform, that platform runs with restore only, and the launch says so.

**Alternative considered:** restore only, as first drafted. It leaves the whole session as a window in which the user's host `git` or `jj git push` honors agent-written config. Rejected as the primary mechanism (user decision, 2026-11-09).

### 3. Snapshot and restore

- **Snapshot:** taken before the container starts, into the session's private state. One shared session per repository means the snapshot can't capture another agent's edits, so no shared baseline is needed.
- **Restore**, for each file that differs from its snapshot:
  1. Open the parent as a directory descriptor (no-follow).
  2. `renameat(name → name.project-sandbox-<session-id>)`, which moves whatever is there, symlink included, without following it.
  3. Write the snapshot through a no-follow temporary file and `renameat` it into place.
- The restore runs first in the `finally` block, after clearing the immutable flags. It also runs on interrupts and timeouts.

### 4. The watcher

- A daemon thread in the CLI process, started after the snapshot and stopped before the restore.
- Every two seconds it builds the manifest of Decision 5 for the protected files, `hooks/`, `info/` and the watched paths, and compares it with the previous one.
- Each difference is appended to the session log as one line, and to the session record's `config_changes` list, with a timestamp.
- It never follows links, never reads outside the listed paths, and never acts on what it finds.
- A change made and reverted between two polls is missed. The end-of-session comparison against the launch manifest still catches anything that persists.

### 5. Manifests and report

- **Manifest:** `{relative path: (type, size, sha256)}`, built with `os.scandir(follow_symlinks=False)`. Symlinks are recorded as links with the hash of their target string. Recursion is capped at 2,000 entries per watched directory. Beyond that, the report says "changed (not enumerated)".
- **`core.hooksPath` directory:** read from the snapshot config. It is watched only when it resolves inside the project.
- **Isolated mode:** the manifests cover the watched agent/IDE paths in the session directory, at launch and at the end. No watcher runs.
- **Exec-capable keys:** the report parses a quarantined Git config with `git config --file <quarantine> --list --null`, run through `host_tools` from a neutral directory. It flags keys matching:
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
- **Printing:** after the egress-denial summary, and as `config_changes: [{path, tree: "workspace"|"git"|"jj", kind, quarantine?, exec_keys?}]` in the summary.

## Risks / Trade-offs

- [The immutable flag or a file bind doesn't hold on some runtime] → Task 1.1 tests each. The fallback is restore plus the watcher, announced at launch.
- [On macOS the user's own `git config`, `git remote add` or `git push -u` fails while a shared session runs] → The error is immediate, and the docs explain it. This replaces the earlier draft's silent revert.
- [A crash leaves files immutable] → Flags are recorded per session and cleared by the next launch and by `doctor`. The docs give the manual `chflags nouchg` command.
- [Agents can't install hooks or change remotes] → Intended. The error comes from Git inside the sandbox.
- [Report noise from IDE files the user edits on the host during a session] → It's a report only. Acceptable.
- [Large watched directories] → Capped enumeration.

## Migration Plan

There's nothing to migrate. On the first shared session after upgrade, the snapshot is the current `.git/config`, which may already contain changes from earlier sessions. The docs suggest running `git config --list --show-origin` once, to review it.
