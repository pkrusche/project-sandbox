# Proposal

## Why

`egress.toml` is the single, global source of the sidecar allowlist, shared by every runtime. Two defects let a user's edit silently lose. This is security finding #7 in TODO.md.

1. **Lost updates.** `project-sandbox-egress allow add|remove` reads, edits and atomically replaces the file without a lock (`egress_policy._edit`). Two concurrent edits both report success, and only one survives. The review reproduced this.
2. **Stale application.** Launches, `up`, `restart` and `reload` load the policy once, then pass that snapshot to `egress_sidecar.ensure()` / `reload()`. Those apply it under the per-runtime sidecar lock. A sidecar-mode launch loads its snapshot before the image build, which can take minutes. If the user runs `allow remove evil.example` in the meantime, the launch later acquires the lock and re-applies its snapshot. The host the user just revoked is then admitted again on the running sidecar, while `egress.toml` says otherwise.

## What Changes

- **Edits are serialized.**
  - `allow add|remove` holds one exclusive lock, keyed by the policy file path, across read, validate, edit and replace. The lock is independent of the runtime.
  - Before replacing the file, the edit checks that its contents are unchanged since the read. This catches a concurrent hand edit in an editor. If the contents changed, the edit is re-applied to the new contents, up to a bound, before failing with a clear message.
- **The sidecar applies only the current file.**
  - Every operation that applies policy to a running or starting sidecar re-reads `egress.toml` from disk while holding the runtime's sidecar lock, and applies what it reads. It never applies a snapshot taken earlier.
  - This covers launch reconcile, cold start, `up`, `restart`, `reload` and the live apply after `allow`.
  - The policy a launch loaded early stays in use for validation, preview and rendering only.
- **Invalid policy at apply time fails safe.** If the re-read file is invalid, the operation fails, the running sidecar keeps its current configuration, and the error names the file.
- **Visibility.**
  - `reload`, and the live apply after `allow`, report which policy content they applied (a short content hash).
  - `status` shows the hash of the policy the running sidecar uses next to the hash of the current file, so drift can be seen.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `egress-sidecar-management`: "Policy edits apply to the running sidecar without a restart" gains concurrency guarantees (no lost updates; the live apply uses the current file) and reports the applied policy hash.
- `egress-sidecar`: "Launches reconcile configuration drift" now states that the applied policy is the file as it is when the sidecar lock is held, not as it was when the launch started. A revoked host is never re-admitted by a delayed launch.

## Impact

- **Code:**
  - `egress_policy.py`: `_edit` gains the path lock, compare-before-replace and retry. A content hash is added to `PolicyFile`.
  - `egress_sidecar.py`:
    - `ensure`, `reload`, `restart` and `up` take a policy loader instead of a `PolicyFile`, and call it inside `_locked()`;
    - the state records the applied hash;
    - `status` reports it.
  - `egress_cli.py` and `cli.py` pass the loader. The launch keeps its early snapshot for `_egress_sidecar_policy` checks and the dry-run preview.
- **Locks:** the new lock uses `host_locks.path_lock("egress-policy", <policy path>)`. `private-session-state` later moves all host locks to a private per-user directory. This change does not depend on that.
- **Lock order:** the policy edit lock is never held while acquiring a sidecar lock. The live apply runs after the edit lock is released and reads the file itself. No deadlock is possible.
- **Behavior:** a hand edit to `egress.toml` that is invalid now makes the next launch fail instead of being ignored in favor of an older snapshot. That is intended.
- **Docs:** `docs/egress-sidecar.md` (concurrency and the hash in `status`).
- **Out of scope:**
  - Terminating tunnels that were already open to a host before it was revoked. Only new connections are affected. This belongs to the networking table's revocation row.
  - The default provider set (`sidecar-policy-hardening`).
