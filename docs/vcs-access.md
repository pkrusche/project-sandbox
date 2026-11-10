# VCS access inside the sandbox

`--vcs-access full|read-only|hidden` controls runtime access to existing `.git`
and `.jj` metadata. The default is `full`, preserving the existing behavior.

```bash
project-sandbox --vcs-access read-only --agent bash /path/to/project python:3.12-slim
project-sandbox --vcs-access hidden --branch task --agent claude /path/to/project python:3.12-slim
```

| Mode | Metadata inside the sandbox | Source files |
| --- | --- | --- |
| `full` | Existing read/write access | Writable |
| `read-only` | Read-only mounts over existing root metadata, worktree pointer files, shared jj stores, and Git backends/common directories | Writable |
| `hidden` | Empty read-only mounts over existing root metadata; no external metadata mounts | Writable |

Hidden mode hides the contents, not necessarily the existence of a `.git` or
`.jj` path. Git/jj commands can report an invalid or unavailable repository.
Agents must edit files without using VCS commands in this mode.

With `--branch`, the host creates the worktree/workspace before launch and
captures changes after the container exits: Git commits dirty files; jj
snapshots and advances the session bookmark. Neither path merges changes into
the main working copy. Existing failure/`--keep-workspace` retention applies.
Restricted sessions record the initial root metadata layout; newly introduced,
removed, or type-changed root metadata prevents automatic capture and leaves
the workspace for inspection.

Without `--branch`, there is no automatic commit/snapshot. The user reviews and
records changes in the original working copy. Existing uncommitted work is
not separated from agent edits by these modes.

## Inspection in read-only mode

Git receives `GIT_OPTIONAL_LOCKS=0` to avoid optional index refresh writes.
Operations requiring metadata writes, including commits, staging and checkout,
fail. jj normally snapshots on command execution; use `--ignore-working-copy`
for inspection, understanding that it shows the previously recorded revision,
not freshly edited files. These environment/command settings are convenience
hints; the read-only mounts enforce the restriction.

## Supported layouts and runtimes

- Docker and Podman support root metadata directories and Git worktree pointer
  files. Generated Docker-compatible devcontainers receive the selected mode.
- Apple container supports directory metadata. Restricted sessions whose root
  `.git` is a file, including Git `--branch` jobs, are rejected because they need
  file bind mounts. Use Docker/Podman for those layouts. jj `--branch` jobs with
  directory `.jj` metadata can use Apple container.
- Chroot, symlinked metadata, and existing nested repositories/submodules
  are rejected in restricted modes. They require additional mount handling.
  Metadata trees containing symlinks are rejected too: a read-only mount would
  not protect a symlink target elsewhere in the writable workspace.
  Hard-linked metadata files are also rejected because an alias outside the
  read-only mount could remain writable. Local clones sharing object files
  through hardlinks need an independent copy before using restricted modes.
- Extra mounts cannot overlap the workspace, expose workspace/metadata aliases,
  or override protected mount targets. Mount forms the parser cannot fully
  inspect are rejected. Commas/control characters in metadata mount paths are
  unsupported.

Hidden mask sources are private directories outside the repository. CLI sources
are removed after the session; generated devcontainer sources remain for that
configuration. Regenerate the devcontainer configuration after temporary-file
cleanup or a reboot that removes those sources. Regenerate after any change to
the repository's metadata layout, and recreate the devcontainer.

## Scope and remaining risks

This is protection for existing runtime-mounted metadata, not sanitization of
an already contaminated repository or a complete host-execution boundary.
Start from trusted metadata. Other host processes or concurrent `full` sessions
can still modify shared metadata; restricted mounts do not provide a snapshot.

`.gitattributes`, `.gitmodules`, `.gitignore`, hook/helper scripts in the working
tree, and other tracked configuration remain visible and editable. In
particular, `.gitattributes` can select an existing configured filter or diff
driver, and a trusted hooks/filter configuration may reference a script the
agent can edit. Host-side Git commands, including automatic finalization, can
therefore still execute workspace-controlled code. Protecting metadata does
not disable these helpers. Review such changes before ordinary host VCS
operations; use private repositories and isolated capture/import when that
stronger boundary is needed.

Paths absent at launch are not created or reserved on the host. An agent can
create new repositories in writable paths, including previously absent `.git`
or `.jj` paths. Branch finalization rejects changes to the root metadata layout;
working-copy sessions require the user's inspection. Newly created nested
repositories are also untrusted and must not be opened automatically.

Builds and image contents are outside this switch: a Dockerfile or base image
may already contain copies of repository history elsewhere. Hidden runtime
mounts do not erase those copies, strip history from build inputs, or prevent
an agent with network access from obtaining another clone.

See [security](security.md) and the outstanding VCS hardening tasks in
[TODO](../TODO.md).
