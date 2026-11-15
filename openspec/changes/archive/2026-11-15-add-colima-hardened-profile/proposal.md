# Proposal

## Why

A default Colima profile exports the user's **entire home directory read-write** into the VM, even when `colima.yaml` says `mounts: []`. Every container the engine runs can be asked to mount `~/.ssh`, `~/.aws` or any other repository, and the VM's root can read all of it. A spike on a throwaway profile (`docs/colima-spikes.md`, section E) showed this is avoidable: project-sandbox bind-mounts only the workspace roots and four subtrees of its own state directory, and a profile that exports exactly those ran a real sidecar launch and the full Colima end-to-end suite (44 of 44) with `~/.ssh`, `~/.aws`, `Documents` and the host-only session records invisible to the VM.

Nothing in the product helps a user get there. `add-colima-runtime` verifies a profile but deliberately does not create one, so the safe configuration is a hand-written `colima start` with about 30 flags, two of which (`--activate=false`, `--ssh-config=false`) protect the user's own Docker context and SSH config, plus a size (a default profile has 2 CPUs, fewer than the sandbox's default of 4). Getting any of it wrong is silent, and `colima start --mount` *replaces* the profile's mounts, so a later "just add a path" can undo the hardening.

## What Changes

- Add `project-sandbox colima init --workspace-root PATH [--workspace-root PATH ...]`, an explicit, one-time command that creates the managed profile (default `project-sandbox`, or `--colima-profile NAME`):
  - Exports exactly the validated workspace roots (read-write) and the state subtrees the sandbox bind-mounts (`session-inputs`, `mounts`, `egress`, `vcs-masks`), nothing else.
  - Starts it with `--activate=false` (the user's Docker context is never switched) and `--ssh-config=false`, Docker runtime, VZ and virtiofs, and a size that fits the sandbox defaults (4 CPUs, 8 GiB, overridable).
  - Prints the exact `colima start` command and asks for confirmation (`--yes` skips it, `--dry-run` only prints). Refuses an existing profile of that name: it never modifies, restarts or deletes one.
- Validate every workspace root before anything runs: an existing absolute directory, resolved through symlinks, that is neither `$HOME`, an ancestor of it, a system directory, nor a credential location (`~/.ssh`, `~/.aws`, `~/.gnupg`, and the like), nor inside the state directory.
- Add `project-sandbox colima add-root PATH`, which reads the profile's current exports from the VM, validates the new root, and after confirmation restarts the profile with the full list plus the new root. It refuses while sandbox containers are running unless `--force`, because the restart interrupts them. This replaces the hand-typed `colima start --mount` that silently drops exports.
- `project-sandbox doctor` warns when a Colima profile exports `$HOME` or an ancestor read-write, naming `colima init` as the way to get a dedicated profile, and reports whether the managed profile's exports are exactly the expected set.
- Document the hardened profile and its trade-offs in `docs/colima.md`; record the remaining spike questions in `docs/colima-spikes.md`.

Out of scope: changing how launches use a profile (a profile that exports `$HOME` keeps working, it only gets a `doctor` warning); `colima start/stop/delete` wrappers beyond `init` and `add-root`; removing a root; per-sandbox isolation; the QEMU driver; any change for users who keep using the `default` profile.

## Capabilities

### New Capabilities

- `colima-hardened-profile`: creating and maintaining a dedicated Colima profile that exports only the workspace roots and the sandbox's own state subtrees, and reporting profiles that export more.

### Modified Capabilities

<!-- None. `colima-runtime` is still inside the unarchived `add-colima-runtime`
     change; this change builds on it and owns its new requirements, including
     the `doctor` warning, rather than writing a delta against a spec that is not
     in `openspec/specs/` yet. Archive `add-colima-runtime` first. -->

## Impact

- Code: a new `colima_profile.py` (root validation, the `colima start` command, exports comparison), a `colima` subcommand dispatched in `cli.py` the way `doctor` and `sessions list` are, `doctor.py`, and a small reuse of `colima_mounts` (`exported_mounts`, `mount_flags`, `required_state_dirs`).
- Behavior: `init` and `add-root` are the only commands that run `colima start`/`stop`, and only after confirmation; launches and `doctor` stay read-only. A profile created by `init` is the only one the project treats as hardened.
- Docs/tests: `docs/colima.md`, `docs/doctor.md`, CHANGELOG; unit tests with a fake runner for every command, plus an opt-in step in `scripts/e2e-colima.py` that runs `init` against a throwaway profile.
- Dependencies: none new. Depends on `add-colima-runtime` being archived.
