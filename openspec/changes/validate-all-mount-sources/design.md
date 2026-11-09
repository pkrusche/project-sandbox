# Design

## Context

- **`parse_mount`** (`container_cli.py:152–195`) splits on `,`, maps a few field aliases, and returns `MountSpec(source, target, readonly, raw)`. Any unknown key, flag or non-bind type sets `raw`, and keeps the string verbatim for the runtime (`_mount_arg`, `:291`).
- **Implicit mounts** are built as strings in `cli.py` (e.g. `:3053–3085`, `:3180–3242`) and in `devcontainer.py` (`:94–182`), then re-parsed. Chroot rejects `raw`.
- **`_egress_sidecar_policy`** (`cli.py:1533`) adds `spec.source` to `protected` only when `raw is None`, then calls `check_location(policy_path, protected)` and `check_location(egress_root, protected)`.
- **`check_location`** (`egress_policy.py:521`) rejects when the *location* is inside a protected root. It doesn't reject a protected *root* that sits inside the location, i.e. a mount of a subdirectory of the state.
- **Runtime syntax:**
  - Docker parses `--mount` as CSV, so double quotes can escape commas.
  - Podman splits on commas.
  - Apple `container`'s `--mount` grammar isn't documented beyond `type=…,source=…,target=…,readonly`.
  - Devcontainer mounts are strings passed to `docker run --mount`.

## Goals / Non-Goals

**Goals:**
- No mount reaches a runtime without passing one validator.
- Protected host locations can't be mounted in any form or egress mode.
- Paths can't inject mount fields.

**Non-Goals:**
- Escaping commas so such paths work. They're refused, because quoting differs per runtime.
- Detecting what a named volume is backed by (Docker volume driver options).
- Stopping users from mounting sensitive host directories in general. Credential directories get a warning, not a refusal.

## Decisions

### 1. A tagged mount model replaces `raw`

```python
@dataclass(frozen=True)
class BindMount:   source: Path; target: str; readonly: bool = False
                   propagation: Literal["private", "rprivate"] | None = None
                   consistency: str | None = None
@dataclass(frozen=True)
class TmpfsMount:  target: str; size: str | None = None; mode: str | None = None
@dataclass(frozen=True)
class VolumeMount: name: str; target: str; readonly: bool = False
Mount = BindMount | TmpfsMount | VolumeMount
```

- **Parser:** splits on commas, which is safe because Decision 4 forbids commas in values. Each item is `key=value` or a bare flag (`readonly`/`ro`). Keys are mapped through the existing aliases. Duplicate keys are refused. Unknown keys and flags are refused with the list of supported ones.
- **Volume names** must match `[A-Za-z0-9][A-Za-z0-9_.-]*`, which also prevents a path posing as a volume name.
- **Implicit mounts** in `cli.py` and `devcontainer.py` construct `BindMount` directly. `_mount_touches_path` compares structured fields.

**Alternative considered:** keep `raw`, and also check sources in raw values. A raw value is by definition not understood, so checking its "source" means guessing the runtime's grammar. Refusing unmodeled forms is the only complete check. The list of supported options is small, and can be extended deliberately.

### 2. Protected locations, checked in both directions

```
protected = { policy dir ($XDG_CONFIG_HOME/project-sandbox),
              egress state root,
              state root ($XDG_STATE_HOME/project-sandbox),
              runtime root (private-session-state) }

refuse if  src == p  or  p in src.parents  or  src in p.parents   (src resolved strictly)
```

- `check_location` gains the reverse direction, and is used for every bind source in every mode, not only by `_egress_sidecar_policy`.
- **Containment:** a source inside the project or workspace must still be inside it after resolution. Otherwise it's refused, catching symlinks an earlier session planted.
- **Credential directories:** the list (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/.password-store`, `~/.claude`, `~/.codex`, `~/.config/opencode`, `~/.pi`, `~/.copilot`) triggers a warning in either direction (the source is one, or contains one).
- **Resolved paths:** the runtime receives the strictly resolved source. A later swap of a symlink component therefore can't redirect the mount.

### 3. Target rules and ordering

- **Normalization:** targets are normalized with `posixpath.normpath`, and must be absolute with no `..` or control characters.
- **Protected target prefixes:** `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/root`, `/proc`, `/sys`, `/dev`, `/run` and `/project-sandbox-*`. These cover what the image and the privileged start use (`isolate-firewall-helpers`). `/opt` and `/home/agent` stay allowed, because mounting caches there is a common legitimate pattern.
- **`/workspace` itself** is refused. Subpaths are allowed.
- **Order:**
  1. the workspace mount;
  2. user mounts;
  3. VCS mounts;
  4. protections: masks, `.git/hooks`/`info` read-only, `.devcontainer` mask.

  The runtime applies later mounts on top, so protections always win.

### 4. One encoder that refuses instead of escaping

`encode(mount) -> str` emits `type=bind,source=<abs>,target=<abs>[,readonly][,bind-propagation=…][,consistency=…]` (and the tmpfs and volume equivalents). Before emitting, it refuses a field value containing `,`, `"`, `\n`, any control character, or `=` (`=` only in paths). `container_cli` (Docker, Podman, Apple) and `devcontainer.py` both use it. A property test checks `parse(encode(m)) == m`.

Paths the CLI itself needs to mount (the project, state and runtime roots, jj paths) go through the same encoder. A project path with a comma is therefore refused with a clear message instead of producing a broken or injected mount.

## Risks / Trade-offs

- [Users relying on unmodeled options (`z`/`Z` SELinux relabeling, `bind-nonrecursive`, volume driver options)] → They get a clear refusal naming the option. Adding an option later means adding it to the model, with a review of its host exposure. SELinux relabeling is deliberately left out, because it changes host file labels.
- [Projects or home directories whose paths contain commas] → They're refused. This is rare, and the message explains why.
- [Credential warnings are noisy for users who deliberately mount `~/.ssh`] → Show the warning once per launch. A suppression flag isn't added, so the risk stays visible.
- [Protected target list too strict for an exotic image layout] → `/opt` and home stay open. The list covers what privileged start-up executes.

## Migration Plan

Mount values that were accepted before and are now refused fail at launch, with the exact rejected part named. There is no persisted state to migrate.
