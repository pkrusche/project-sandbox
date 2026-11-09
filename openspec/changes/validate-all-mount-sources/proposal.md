# Proposal

## Why

`--mount` values reach the container runtime with only partial checks:

- **Raw passthrough bypasses checks (finding #5).**
  - `container_cli.parse_mount` splits on commas, and keeps any value it doesn't fully model (an extra option such as `bind-propagation=rprivate`, a volume, a tmpfs) as `raw`.
  - `_egress_sidecar_policy` only checks sources when `raw is None`, so adding one option to an otherwise rejected bind of the policy or sidecar-state directory gets it accepted.
  - The raw string then reaches the runtime unchanged.
- **Subdirectories of protected locations pass (new finding).** `egress_policy.check_location` rejects a mount that *contains* the policy file or the sidecar state directory, but not one *inside* it. `--mount source=<egress state>/docker/sidecar,…` mounts the sidecar CA private key into a sidecar-mode session.
- **Protection only in sidecar mode.** Policy and state locations are only protected in sidecar mode. The CA key is shared by every sidecar session, so a firewall-mode session has no reason to see it either.
- **Mount encoding.**
  - `_mount_arg`, `devcontainer.json` mounts, and the CLI's own implicit mounts concatenate paths into comma-separated strings, which are re-parsed by the same comma split.
  - A path containing `,` adds or replaces mount fields.
  - JSON escaping and argv lists don't protect the runtime's nested mount syntax (TODO.md "Mount encoding").
- **Targets.** Any target is accepted, including image-managed locations that privileged startup executes (`/usr/local/bin`, `/etc`). A writable source mounted there, for example a workspace subdirectory, puts agent-controlled files where the root stage runs them.

Implicit mount *sources* are validated by `private-session-state` and `jj-only-branch-workspaces`. This change covers user `--mount` values, and makes every mount, implicit or not, go through one structured model and one encoder.

## What Changes

- **One mount model, no raw passthrough.**
  - `--mount` is parsed into a structured value.
  - Supported forms: bind mounts (`source`, `target`, `readonly`/`ro`, and the harmless options `bind-propagation=private|rprivate` and `consistency=…`), tmpfs mounts (`target`, `tmpfs-size`, `tmpfs-mode`) and named volumes (see below).
  - Anything else, including unknown keys and options, fails with an error that lists the supported forms.
  - Implicit mounts are built as structured values directly, never as strings.
- **Every bind source is checked, in every egress mode.** After strict resolution, a source is refused when:
  - it is, contains or lies inside the egress policy directory, the egress state directory, or project-sandbox's state and runtime roots (`private-session-state`);
  - it lies inside the project or workspace but resolves outside it, for example a symlink an earlier session planted.

  Mounting a well-known credential directory (`~/.ssh`, `~/.gnupg`, `~/.aws`, the agents' config directories, `~/.config/gh`, `~/.password-store`) is allowed, with a warning that it bypasses per-agent credential filtering. The *resolved* path is what's passed to the runtime.
- **Named volumes.** Their host exposure can't be determined (a volume can be backed by an arbitrary host directory). They are refused in sidecar mode, and allowed with a warning in firewall mode.
- **Targets are checked.**
  - Absolute, no `..`, no control characters.
  - Not `/workspace` itself, and not under `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/root`, `/proc`, `/sys`, `/dev`, `/run` or `/project-sandbox-*`.
  - Masks and protections that project-sandbox adds stay last, so they still win over user mounts beneath them.
- **One encoder.**
  - Structured mounts are rendered by a single encoder for the runtimes' `--mount` syntax and for `devcontainer.json`.
  - Any field containing `,`, `"`, `=` in a path, or a control character is refused rather than escaped. The runtimes' quoting rules differ, and Apple's are undocumented.
  - A parse/render round-trip test covers unusual names.

## Capabilities

### New Capabilities

- `mount-validation`: the supported `--mount` forms, source and target rules, the protected locations, how volumes are treated, and how every mount is encoded for the runtimes and the devcontainer.

### Modified Capabilities

- `egress-sidecar`: "Policy lives on the host outside the sandbox's reach" changes:
  - mounts *inside* the policy or state directory are rejected too;
  - every mount form is checked, with no raw bypass;
  - the check applies to all egress modes, because the state directory is never mountable.

## Impact

- **Code:**
  - `container_cli.py`: `MountSpec` becomes a tagged union (bind, tmpfs, volume) with `raw` removed; `parse_mount` uses a real key/value grammar; a new encoder; validation.
  - `cli.py`: implicit mounts built as `MountSpec`; validation for all modes; `_mount_touches_path` uses the model.
  - `egress_policy.check_location`: checks both directions.
  - `devcontainer.py`: mounts rendered from `MountSpec`.
  - `chroot`: unchanged (bind only).
- **BREAKING:** `--mount` values with unsupported options, tmpfs or volume forms the model doesn't cover, commas in paths, or targets in protected locations are refused. Error messages name the rejected part and the supported alternative.
- **Depends on:** `private-session-state` (state and runtime roots and their implicit mounts) and `jj-only-branch-workspaces` (jj mounts as computed values). Both feed structured mounts into the encoder.
- **Docs:** `docs/usage.md` (`--mount` forms and rules), `docs/security.md` (protected locations, the credential-directory warning, volumes).
