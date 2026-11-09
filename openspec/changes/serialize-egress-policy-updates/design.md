# Design

## Context

```
allow add/remove                       launch (sidecar mode)
────────────────                       ─────────────────────
read egress.toml          ┐            P1 = load_global()          (cli.py:1553, early)
edit in memory            │ no lock    ... render, build image (minutes) ...
_replace_atomically       ┘            ensure(runtime, P1)         (cli.py:1591)
reload(runtime, P2) ── sidecar lock ──   └ _locked(): _reconcile(P1)  ← stale snapshot wins
```

- `egress_policy._edit` (`egress_policy.py:578`) is a plain read-modify-replace.
- `_replace_atomically` uses `mkstemp` and `os.replace`, so readers never see a partial file. But two editors can each replace the file with an edit based on the same original.
- `egress_sidecar._locked` (`:890`) serializes sidecar mutations per runtime under `$XDG_STATE_HOME/project-sandbox/egress/<runtime>/`. Every entry point receives a `PolicyFile` built before the lock was taken:
  - `ensure` (`:920`), `reload` (`:1397`) and `restart`;
  - in `egress_cli`, `ctx.policy()` (`:135`), which is called before each operation.
- After `ensure`, the launch re-checks `sidecar.copilot` and the port against the *running* sidecar (`cli.py:1606`).

## Goals / Non-Goals

**Goals:**
- No acknowledged `allow` edit is lost.
- What a sidecar runs is always derived from the file's contents at a moment the sidecar lock was held.

**Non-Goals:**
- Cross-runtime atomicity, meaning all runtimes' sidecars switching at once. Each runtime still converges at its next apply. The existing message about other runtimes stays.
- Closing connections that are already established, after a revocation.
- Protecting against a hand edit and an `allow` command racing inside the few microseconds between the compare and the replace (see Risks).

## Decisions

### 1. Appliers re-read the file under the sidecar lock

`ensure`, `reload`, `restart` and `up` take `load_policy: Callable[[], PolicyFile]`, with `egress_policy.load_global` as the default, and call it as the first step inside `_locked()`. The `PolicyFile` argument goes away from these signatures, so a stale snapshot cannot be passed by mistake.

**Why this is enough without generations:**
- The applied content always comes from a read made while the lock is held, after every replace that finished earlier.
- So the last apply to finish uses content at least as new as any earlier apply.
- Every `allow` command applies live after its replace, so the final state equals the final file.
- This is last-writer-wins on file contents, not on snapshots.

**Alternative considered:** generations or hashes in the file, with stale applies refused. That needs a writer-side counter in a user-editable TOML file, and it still needs the re-read to know the current generation. Rejected as more machinery for the same outcome. The hash in Decision 4 is for diagnostics only.

### 2. One edit lock keyed by the policy path, with compare-before-replace

`_edit` runs inside `host_locks.path_lock("egress-policy", path)`, keyed by the resolved policy path and independent of the runtime. Inside the lock:

1. read the bytes and parse them;
2. apply the change and validate the result;
3. re-read the bytes;
4. if they still equal step 1, replace atomically;
5. otherwise, the file was changed outside project-sandbox, typically by an editor. Restart from step 1, up to 3 attempts, then fail with "egress.toml changed while editing; retry".

`remove` of a host that a concurrent hand edit already removed fails on the retry with the existing "Not in egress.allow" error. The user sees an accurate state.

**Lock order:** the edit lock is released before the live apply. Appliers never take the edit lock, because the atomic replace makes their reads safe. So there is no lock-order cycle between the edit lock and the per-runtime locks.

### 3. Launch requirements are re-checked against the applied policy

`ensure` returns the `PolicyFile` it applied, along with the `Sidecar`. `_ensure_egress_sidecar` re-runs the policy-dependent launch checks against that policy:
- the Copilot profile is required for `--agent copilot`;
- the port must match the rendered one, which is already covered by `expected_port`.

The early `_egress_sidecar_policy` load remains. It drives validation that has to happen before rendering (location checks and rendering the port) and the `--dry-run` preview. It is never applied.

The Copilot check now requires the profile in *both* the applied file and the running sidecar. A user who disabled the profile has stated an intent, and a pending restart should not override it.

### 4. A content hash for observability

`PolicyFile` gains `digest`: the first 12 hex characters of the SHA-256 of the file bytes, or of an empty document when the file is missing. `_reconcile` and `_cold_start` record `policy_digest` in the sidecar state. `reload` and the live apply print "Applied egress.toml <digest>". `status` prints the running digest and the current file's digest, and marks the difference when they disagree.

## Risks / Trade-offs

- [An editor saves between the compare (step 3) and `os.replace` (step 4)] → The window is the duration of one `mkstemp`, write and rename. The editor's save would be lost. This is documented. Fully closing it would need the editor to cooperate with the lock.
- [An invalid hand edit now blocks launches that previously proceeded on the old snapshot] → The behavior is intended and the error names the file. Before this change, such a launch applied an allowlist that no longer existed on disk.
- [Lock files in shared temp storage, from the review's hardening list] → This reuses `host_locks` as it is today. `private-session-state` relocates every lock to a private per-user directory. That's an independent step.
- [A long-held sidecar lock during a cold start delays a concurrent `allow`'s live apply] → Same as today. The edit itself is not blocked, because it doesn't take the sidecar lock.

## Migration Plan

There's no data migration. The state file gains an optional `policy_digest`. A sidecar started before this change shows "unknown" until its next apply.
