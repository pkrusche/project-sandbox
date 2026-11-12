# Design

## Context

- **Full-access sessions** (`--vcs-access full`, the default): without `--branch`, the project including `.git/` and `.jj/` is bind-mounted read-write at `/workspace`; with Git `--branch`, the main `.git` is mounted read-write at its host path; with jj `--branch`, the main `.jj/repo` store is mounted read-write. Several full sessions may run on one repository at once.
- **`read-only`/`hidden` and isolated sessions** already keep the main metadata unwritable or unmounted.
- **Apple `container`** 1.7.1: `--mount` rejects single-file binds ("is not a directory"). `-v` accepts a file, but it shares the file's parent directory and silently drops a directory mount of that same parent. Nested directory binds work.
- **Host jj** has `git.executable-path = "git"`, so `jj git push` and `fetch` run Git against `.git/config`.
- **Session teardown:** happens in `cli.main`'s `finally` block, ending with the JSON summary.

## Goals / Non-Goals

**Goals:**
- An agent in a full-access session can't leave behind Git hooks, Git config or jj config pointers that the user's host tools will honor.
- Where the platform allows, it can't even change them while the session runs.
- The user learns about every change to config their host tools may run automatically, as it happens in `full` mode and at the end in every mode.

**Non-Goals:**
- Preventing agents from editing project files, including the watched ones.
- Inspecting committed content (`Makefile`, build scripts).

## Decisions

### 1. Protection per path

| Path | Mechanism | Why |
|---|---|---|
| `.git/`, `.jj/`, `.jj/repo/` | read-write bind of the same host directory over itself, before the protections below | A mount point can't be renamed. Without it, the agent renames the parent away and recreates it with its own config, which defeats every protection below (spike, Decision 2). |
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

**Spike, 2026-11-09** (task 1.1; Docker Desktop 29.9.3, Podman 6.0.0 machine (applehv, rootless, `--userns=keep-id`), Apple `container` 1.7.1, macOS host on APFS). In a throwaway `git init` repository mounted read-write at `/workspace`, the container tried: `chmod`, `touch`, append, `truncate`, `git config core.fsmonitor …`, rename a file over `config`, rename `config` away, delete it, hard-link it, and finally rename `.git` away, recreate it and write its own `config`. Each case ran as root with `NET_ADMIN`/`NET_RAW`, and the self-bind cases also ran as the agent user.

| Mechanism | Docker Desktop | Podman machine | Apple `container` |
|---|---|---|---|
| read-only file bind of `config` | all file operations blocked (`EROFS`, `EBUSY`, `EXDEV`) | same | same, with `-v` only |
| `chflags uchg` on the host file | all file operations blocked (`EPERM`) | same | same |
| either of the above, but no `.git` self-bind | **bypassed**: `.git` renamed and recreated with an agent-written `config` | **bypassed** | **bypassed** |
| file bind plus `.git` self-bind | holds (`mv .git` gives `EBUSY`) | holds | **bypassed**: Apple drops the `.git` mount (Context) |
| `uchg` plus `.git` self-bind | holds | holds | holds |

- `.git` self-bind plus read-only `hooks/` and `info/` binds plus `uchg` on `config` blocks writing a hook, writing `info/attributes`, renaming or removing `hooks/`, renaming `.git`, and appending to `config`, on all three runtimes.
- Neither runtime grants `CAP_LINUX_IMMUTABLE`, and the host enforces `uchg` regardless of the container's uid.
- **Consequences:**
  - The self-binds of Decision 1 are required on every runtime. Renaming `.jj` or `.jj/repo` was not tested, but it is the same `rename(2)` case.
  - Use `uchg` on all three macOS runtimes. Apple has no working file bind next to a `.git` self-bind.
  - On native Linux, use the read-only file bind plus the self-bind. It held in both Linux VMs, but a native Linux host was not tested.
- **Fallback platforms:** none among those tested. Restore-only applies where `chflags` fails, such as volumes without BSD flags (exFAT, some network shares; untested), and to runtimes not listed here.

**Alternative considered:** restore only, as first drafted. It leaves the whole session as a window in which the user's host `git` or `jj git push` honors agent-written config. Rejected as the primary mechanism (user decision, 2026-11-09).

### 3. Snapshot and restore

- **Baseline:** one per repository, in private state, under a per-repository lock with a reference count of running full sessions. The first full session takes it before its container starts; later concurrent sessions reuse it, so no session snapshots another agent's edits. The last session to end restores and drops it. Restoring at an earlier session's end would undo nothing a later session couldn't redo, so earlier sessions only report.
- **Restore**, for each file that differs from its snapshot:
  1. Open the parent as a directory descriptor (no-follow).
  2. `renameat(name → name.project-sandbox-<session-id>)`, which moves whatever is there, symlink included, without following it.
  3. Write the snapshot through a no-follow temporary file and `renameat` it into place.
- The restore runs first in the last session's `finally` block, after clearing the immutable flags. It also runs on interrupts and timeouts. A crashed session's reference is dropped by the next launch when its lease is free.

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
- [On macOS the user's own `git config`, `git remote add` or `git push -u` fails while a full-access session runs] → The error is immediate, and the docs explain it. This replaces the earlier draft's silent revert.
- [A crash leaves files immutable] → Flags are recorded per session and cleared by the next launch and by `doctor`. The docs give the manual `chflags nouchg` command.
- [Agents can't install hooks or change remotes] → Intended. The error comes from Git inside the sandbox.
- [Report noise from IDE files the user edits on the host during a session] → It's a report only. Acceptable.
- [Large watched directories] → Capped enumeration.

## Migration Plan

There's nothing to migrate. On the first full-access session after upgrade, the snapshot is the current `.git/config`, which may already contain changes from earlier sessions. The docs suggest running `git config --list --show-origin` once, to review it.
