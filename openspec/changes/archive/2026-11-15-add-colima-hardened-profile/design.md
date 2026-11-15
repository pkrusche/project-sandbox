# Design

## Context

See `proposal.md` for motivation and `docs/colima-spikes.md` section E for the evidence. `add-colima-runtime` (archive it first) gives a launch a verified, explicitly targeted profile and the building blocks this change reuses: `colima_mounts.exported_mounts()` (the VM's current exports), `mount_flags()` (a `--mount` list that keeps them), `required_state_dirs()` (the state subtrees the sandbox bind-mounts), and `doctor`'s Colima section.

Facts established on a real profile (Colima 0.10.3, VZ):

- Explicit `--mount` flags produce exactly those exports and no others. There is no implicit `/tmp/colima` and no home mount.
- `colima start --mount` on restart **replaces** the saved mounts. It never adds.
- `--activate=false` keeps the user's Docker context. `--ssh-config=false` stops the `~/.ssh/config` include but Colima still writes its own `~/.colima/ssh_config`, which `colima delete` cleans.
- A mount source must exist on the host; the sandbox's state subtrees are created lazily, so init has to create them.
- `mount_storage.storage_root()` returns a symlink-resolved path while the other state paths are not resolved.

## Goals / Non-Goals

**Goals:**
- One explicit command that produces a profile exporting only what the sandbox mounts, with the user's context, SSH config and existing profiles protected.
- A safe way to change the exports later that cannot drop them.
- `doctor` makes the exposure visible for every profile, including ones this change never creates.

**Non-Goals:**
- Enforcement at launch: a profile that exports `$HOME` keeps working.
- A general Colima manager (start, stop, delete, reset), removing a root, or migrating images and volumes between profiles.
- The QEMU driver (untested), and per-sandbox isolation.

## Decisions

### D1. A `colima` subcommand dispatched before argument parsing

`cli.py` already dispatches `doctor` and `sessions list` from `raw_argv` before the positional parser runs. `colima init` and `colima add-root` follow that: `raw_argv[:1] == ["colima"]` hands the rest to `colima_profile.main()`. The positional CLI stays unchanged.

*Alternative:* a separate `project-sandbox-colima` executable (as `project-sandbox-egress` is). Rejected for now: two verbs do not justify another entry point, and `doctor` sets the precedent.

### D2. The command is data, shown before it runs

`colima_profile.build_start_argv(profile, roots, state_dirs, size)` is a pure function returning the argv list. `init` prints `shlex.join(argv)`, asks for confirmation (`--yes` skips it, a non-interactive run without `--yes` is an error), then runs it with the terminal attached so Colima's progress is visible. `--dry-run` prints and stops. Every behavior in the spec (exports, flags, sizes, refusals) is therefore testable on the argv without a VM.

### D3. One source of truth for the state exports

The four state subtrees come from `colima_mounts.required_state_dirs()`, the same function `doctor` uses to decide what a profile must make visible. Init creates each with mode 0700 first. If any of them differs from its symlink-resolved path (`XDG_STATE_HOME` behind a symlink), init refuses and names the resolved directory, because the export and the sandbox's `source=` string would not match and the launch preflight would then report the path as missing. This also avoids the inconsistency noted above rather than papering over it.

### D4. Root validation is a deny list over resolved paths

`validate_root(path)` resolves symlinks, then refuses with a named reason: not an existing absolute directory; the home directory or an ancestor of it; a system directory (`/`, `/Users`, `/System`, `/Library`, `/private`, `/var`, `/etc`); a credential location or a path containing one (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.docker`); or inside the sandbox state root. It is a deny list because the purpose is to stop the two likely mistakes (exporting `~` or a credentials directory) without second-guessing where a user keeps projects. Symlinks inside an exported root that point at host paths outside it do not resolve in the VM, since the target is not mounted there.

*Alternative:* an allow list (for example, only under `~/src`). Rejected: it encodes one person's layout.

### D5. `add-root` rebuilds the full list, never appends

`add-root` reads `exported_mounts()`; if it cannot, it stops (the spec forbids passing the new path alone). It validates the new root, builds `mount_flags(exports, [root])`, and after confirmation runs `colima stop` then `colima start` with that list, always re-passing `--activate=false --ssh-config=false` because the flags apply per start. It counts running project-sandbox containers first (`docker --context <ctx> ps`, excluding the sidecar, which the next launch recreates) and refuses without `--force`, since the restart ends them.

### D6. A host-only record of the chosen roots

`doctor` cannot tell an intended workspace root from an unexpected export by looking at the VM, because the roots are the user's data. Init and `add-root` write `<state root>/colima-profiles/<profile>.json` (mode 0600, paths only). That directory is deliberately not among `required_state_dirs()`, so it is never exported. `doctor` compares exports with `roots ∪ required_state_dirs()`; without a record (a profile made by hand) it reports only the `$HOME` check.

### D7. Doctor warns; launches do not refuse

Enforcement would break everyone on the `default` profile and the e2e suite's target. `doctor` gets two findings: any profile exporting `$HOME` or an ancestor read-write (warn, naming `colima init`), and, for a profile with a record, extra or missing exports (warn, naming each path).

### D8. Check the flags exist before running

`colima start --help` is read first and must list `--activate`, `--ssh-config`, `--mount`, `--mount-type` and `--vm-type`; otherwise init stops and names the missing flag. A future Colima that renames one then fails clearly instead of starting a VM with the user's context switched.

## Risks / Trade-offs

- **[The VM's root can still read everything exported]** → Unchanged boundary, now narrow. The exports include `session-inputs`, where credentials are staged for the life of a session, and `egress`, which holds the sidecar CA key. Documented, not mitigated.
- **[`add-root` restarts the VM]** → The running-sandbox check and `--force`. The sidecar and networks survive a restart; sandboxes do not.
- **[Colima changes flag behavior]** → D8, plus argv-level tests pinned to the 0.10.3 help text.
- **[`--ssh-config=false` still writes `~/.colima/ssh_config`]** → Documented; `colima delete` removes it. Not a change to `~/.ssh/config`.
- **[A user adds a root by hand with `colima start --mount`]** → The record goes stale and the exports shrink. `doctor` reports the drift; `add-root` is the documented path.
- **[Moving from `default` to the managed profile]** → Separate engines: images and volumes do not carry over, so the first launch rebuilds. Documented.

## Migration Plan

Additive. Nothing changes for an existing profile or for launches. A user who wants the hardened profile runs `project-sandbox colima init --workspace-root …`, then `--runtime colima` as before; the old profile can be left or deleted with Colima's own commands. Rollback is `colima delete --profile project-sandbox`.

## Open Questions

- Whether a later `colima remove-root` is wanted. It would use the same read-rebuild-restart path, so nothing here blocks it.
- Whether a launch should print a one-line note when its profile exports `$HOME`. It would not change any requirement; `doctor` covers the need for now.
