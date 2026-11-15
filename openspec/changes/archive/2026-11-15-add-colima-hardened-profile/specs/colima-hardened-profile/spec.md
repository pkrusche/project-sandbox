# Spec Delta

## Purpose

Lets a user create and maintain a dedicated Colima profile that exports only the workspace roots and the sandbox's own state subtrees, instead of the whole home directory, and reports profiles that export more.

## ADDED Requirements

### Requirement: Init creates a profile that exports only what the sandbox mounts
`project-sandbox colima init --workspace-root PATH` (repeatable) SHALL create the managed Colima profile (default `project-sandbox`, or `--colima-profile NAME`) with exactly these exports: each validated workspace root read-write, and the sandbox's state subtrees `session-inputs`, `mounts`, `egress` and `vcs-masks` read-write. The profile SHALL NOT export the home directory, the session records, locks, or any other path. At least one workspace root SHALL be required.

#### Scenario: A profile with the exact exports
- **WHEN** the user runs `colima init --workspace-root ~/src` and confirms
- **THEN** the profile's exports are `~/src` and the four state subtrees, and a container cannot see `~/.ssh` or the session records

#### Scenario: No workspace root
- **WHEN** the user runs `colima init` without `--workspace-root`
- **THEN** the command exits before running anything, naming the missing option

#### Scenario: State subtrees are created first
- **WHEN** a state subtree does not exist yet
- **THEN** init creates it with owner-only permissions before the profile starts, because a mount source must exist

### Requirement: The chosen roots are recorded on the host
Init and `add-root` SHALL record the profile's workspace roots in a file under the sandbox state root that is not exported to the VM, so that `doctor` can tell expected exports from unexpected ones. The record SHALL be owner-readable only and SHALL hold paths, never credentials.

#### Scenario: Roots recorded by init
- **WHEN** init completes with `--workspace-root ~/src`
- **THEN** the record lists `~/src`, and the file is not under any exported path

#### Scenario: Roots updated by add-root
- **WHEN** add-root completes for `~/work`
- **THEN** the record lists both `~/src` and `~/work`

### Requirement: Init never changes the user's environment or an existing profile
Init SHALL start the profile with `--activate=false` and `--ssh-config=false`, so the user's active Docker context and `~/.ssh/config` are not changed. It SHALL print the exact `colima start` command and require confirmation, or `--yes`. `--dry-run` SHALL print it and run nothing. Init SHALL refuse a profile name that already exists in any state, and SHALL NOT stop, restart, reconfigure or delete it.

#### Scenario: Dry run
- **WHEN** the user passes `--dry-run`
- **THEN** the command is printed, no directory is created and `colima` is not run

#### Scenario: Profile already exists
- **WHEN** a profile named `project-sandbox` exists, running or stopped
- **THEN** init exits before running anything and names `add-root` for changing its exports

#### Scenario: Active context is untouched
- **WHEN** init completes while the active Docker context is `colima`
- **THEN** the active context is still `colima` and the new `colima-project-sandbox` context is not active

#### Scenario: Declined confirmation
- **WHEN** the user answers no at the confirmation
- **THEN** nothing runs and the exit status is non-zero

### Requirement: Workspace roots are validated and canonicalized
Init and `add-root` SHALL resolve each root through symlinks and refuse one that is not an existing absolute directory, is the home directory or an ancestor of it, is a system directory (`/`, `/Users`, `/System`, `/Library`, `/private`, `/var`, `/etc`), is or contains a credential location (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.docker`), or is inside the sandbox state directory. Each refusal SHALL name the rule. The resolved path SHALL be the one exported.

#### Scenario: Home directory refused
- **WHEN** the user passes `--workspace-root ~`
- **THEN** init refuses it, saying the home directory is what a dedicated profile avoids exporting

#### Scenario: Credential location refused
- **WHEN** the user passes `--workspace-root ~/.ssh`
- **THEN** init refuses it and names the credential rule

#### Scenario: Symlink resolved
- **WHEN** a root is a symlink to `/Volumes/work/src`
- **THEN** the profile exports `/Volumes/work/src`

### Requirement: Init refuses a state directory behind a symlink
The sandbox bind-mounts its state subtrees by the path it computes, and the VM exports the path it is given. Init SHALL refuse when any state subtree's path differs from its symlink-resolved path, naming the subtree and the resolved path to use as `XDG_STATE_HOME`, because the export would not match the mount source.

#### Scenario: Symlinked state home
- **WHEN** `XDG_STATE_HOME` is a symlink to another directory
- **THEN** init exits before running anything and names the resolved directory to use instead

### Requirement: The profile is sized for the sandbox defaults
Init SHALL start the profile with 4 CPUs and 8 GiB of memory so a default sandbox launch is not refused by Docker, and SHALL accept `--cpu`, `--memory` and `--disk` to override them.

#### Scenario: Default size
- **WHEN** init runs without size options
- **THEN** the printed command passes 4 CPUs and 8 GiB

#### Scenario: Override
- **WHEN** the user passes `--cpu 2 --memory 4`
- **THEN** the printed command passes 2 CPUs and 4 GiB

### Requirement: Add-root keeps the current exports
`project-sandbox colima add-root PATH` SHALL read the profile's current exports from the VM, validate the new root, and after confirmation restart the profile with every current export plus the new root, preserving each export's access mode. It SHALL refuse to run when the current exports cannot be read, and SHALL NOT pass the new path alone.

#### Scenario: Existing exports survive
- **WHEN** the profile exports `~/src` and the state subtrees and the user adds `~/work`
- **THEN** the restart passes all of them plus `~/work`, and the profile exports each one afterwards

#### Scenario: Exports cannot be read
- **WHEN** the VM is stopped or `colima ssh` fails
- **THEN** add-root exits before restarting anything, saying why

#### Scenario: Already exported
- **WHEN** the path is already exported
- **THEN** add-root reports that and changes nothing

### Requirement: Add-root does not interrupt running sandboxes
`add-root` SHALL refuse while project-sandbox containers are running in the profile, because restarting the VM ends them, unless the user passes `--force`. It SHALL print how many are running and that they will be lost.

#### Scenario: Sandbox running
- **WHEN** a sandbox container is running and the user runs `add-root PATH`
- **THEN** the command exits before restarting, naming the running containers and `--force`

### Requirement: Doctor reports profiles that export the home directory
`project-sandbox doctor` SHALL warn when a Colima profile exports the home directory or an ancestor of it read-write, naming `colima init` as the way to get a dedicated profile. For the managed profile it SHALL also report whether its exports are exactly the expected set, and name any extra or missing export. It SHALL NOT change a profile.

#### Scenario: Default profile exports home
- **WHEN** the profile exports `/Users/me` read-write
- **THEN** doctor warns that the whole home directory is readable and writable from the VM and names `project-sandbox colima init`

#### Scenario: Managed profile as expected
- **WHEN** the managed profile exports exactly its workspace roots and the four state subtrees
- **THEN** doctor reports the exports as dedicated, with no warning

#### Scenario: Managed profile has drifted
- **WHEN** the managed profile also exports `/Users/me/Documents`
- **THEN** doctor warns and names `/Users/me/Documents` as unexpected
