## Purpose

Host-side advisory locks serialize project-sandbox runs that mutate shared host state (repository stores, worktree registries, build contexts, image caches, egress policy). This capability defines where those locks live and who can reach them, so that no other local user or sandbox can block or redirect them.

## ADDED Requirements

### Requirement: Host locks live in a private per-user directory
Every host-side lock file SHALL be created in `$XDG_STATE_HOME/project-sandbox/locks` (default `~/.local/state/project-sandbox/locks`; a relative or empty `XDG_STATE_HOME` is ignored). The directory SHALL be owned by the invoking user with mode `0700`, created if missing, and lock files SHALL be created `0600`. No lock file SHALL be created in the system temp dir.

#### Scenario: Locks created in the state root
- **WHEN** a run takes a build, image, workspace, VCS or egress-policy lock with `XDG_STATE_HOME` unset
- **THEN** the lock file is under `~/.local/state/project-sandbox/locks` and nothing named `project-sandbox-*.lock` is created in the system temp dir

#### Scenario: Missing directory created private
- **WHEN** the locks directory does not exist and the umask is `022`
- **THEN** the run creates it with mode `0700` before taking the lock

#### Scenario: Another user cannot interfere
- **WHEN** another local user tries to pre-create, open or `flock` a lock file for one of our projects
- **THEN** they cannot reach the locks directory, and our launch neither fails with `EACCES` nor blocks on their lock

### Requirement: Unsafe lock storage is refused
A run SHALL refuse to take a lock when the locks directory or any of its ancestors up to the filesystem root is a symlink, or when the locks directory is not owned by the invoking user or is accessible by group or others. A run SHALL also refuse an existing lock name that is not a regular file owned by the invoking user. The error SHALL name the offending path. The run SHALL NOT fall back to the system temp dir or any other shared location.

#### Scenario: Locks directory is a symlink
- **WHEN** `~/.local/state/project-sandbox/locks` is a symlink
- **THEN** the run exits with an error naming that path and creates no lock file

#### Scenario: Locks directory is group-writable
- **WHEN** the locks directory has mode `0770`
- **THEN** the run exits with an error naming that path and does not tighten or reuse it

#### Scenario: Lock name is not a regular file
- **WHEN** a FIFO exists at a lock file's name inside the locks directory
- **THEN** the run exits with an error naming the lock path and does not enter the locked section

### Requirement: Lock identity is independent of path spelling
A lock SHALL be keyed by its kind and the symlink-resolved path of the target it protects, so that every run serializes on the same lock however the target path was spelled.

#### Scenario: Symlinked repository path
- **WHEN** one run names a repository through a symlink and a concurrent run names its resolved path
- **THEN** both runs use the same lock file, and the second waits for the first

### Requirement: Host locks are never visible to a sandbox
The locks directory SHALL NOT be mounted into any sandbox, used as a build context, or exported by a Colima profile.

#### Scenario: Colima profile exports
- **WHEN** `project-sandbox colima init` creates a profile
- **THEN** the locks directory is not among its exports

#### Scenario: Sandbox mounts
- **WHEN** a sandbox starts on any runtime
- **THEN** no mount source is the locks directory or inside it
