# Review: agent credential separation (`main..@`)

Scope: OpenSpec change `agent-separation` plus implementation commits
`Implement agent credential separation` and
`Harden credential mount policy tests`.

Verification run: `uv run python -m compileall src tests` clean;
`uv run pytest -q` — 505 passed, 4 skipped, 43 subtests passed.

## What checks out

- **Two-layer enforcement works as designed.** `filter_credential_dirs`
  filters at the CLI (`src/project_sandbox/cli.py:1517`), and
  `build_mount_specs` independently re-checks via
  `allowed_credential_agents(agent)`
  (`src/project_sandbox/container_cli.py:187`). Removing the `agent="bash"`
  default from `build_mount_specs` is a genuine hardening — the previous
  default made a forgetful caller fail *open* to all credentials; the new
  `inspect.signature` test pins the no-default contract.
- **Chroot stays multi-agent safely.** `run_mode_agent` is passed into the
  chroot mount build, but `_validate_chroot_session`
  (`src/project_sandbox/cli.py:785`) rejects anything but `--agent bash`, so
  only bash/bash-headless ever reach it — both allow all credentials.
- **Token-lifetime warning is unaffected.** It receives the unfiltered
  credential map, but `staged_token_expiry` filters by agent internally.
- **Dry-run converges** into `_build_session_command`, so it is filtered
  identically. Devcontainer rendering keeps the full map, matching the
  documented intentionally-multi-agent design.
- **Docs are accurate for the runtime path.** Provisioning in
  `_provision.sh.j2` is presence-driven, so "only the selected agent's
  mounted credentials are copied" holds.

## Findings

### 1. `tasks.md` marks test tasks done that do not exist (main issue)

The changeset only touches `tests/test_cli.py`, `tests/test_container_cli.py`,
and the new `tests/test_credential_policy.py`, yet:

- **Task 3.4** claims chroot command/mount credential tests were added. No
  test anywhere combines chroot with credentials/secrets (grep across the
  suite: zero hits). The chroot path's `agent=` wiring is entirely untested.
- **Task 4.3** claims renderer/build assertions that staged credentials never
  reach image layers. No such assertion exists; `tests/test_renderers.py` is
  untouched. This leaves the spec scenario "Built image contains no staged
  credentials" (and the matching `docs/security.md` claim about the image
  build context) without regression coverage.
- **Task 3.5** claims devcontainer tests were "updated";
  `tests/test_devcontainer.py` is unchanged. Pre-existing tests do assert all
  credential mounts, so coverage exists, but the checkbox describes work that
  did not happen. Tasks 4.1/4.2 similarly say "Add regression tests" for
  behavior covered only by pre-existing tests.

### 2. New test fixture violates the invariant the change documents

`test_named_sessions_mount_only_their_own_credentials_in_all_modes`
(`tests/test_cli.py`) stages credentials at `context_dir / "staged" / <agent>`
— inside `.project-sandbox`, the exact location the design declares
credentials must never live (and which the workspace mask hides). Functionally
harmless, but it models a forbidden layout; staging under a tmp dir outside
the project would match reality.

### 3. Silent fail-closed (design note, not a bug)

An unknown runtime mode gets `frozenset()` from
`allowed_credential_agents` — no error, no credentials. Safe direction and
consistent with the decision recorded in `design.md`, but a future dispatch
mode added without a policy update will launch an agent silently
unauthenticated, surfacing as a confusing in-container "please log in" rather
than a clear failure. A `raise` for unknown non-bash modes would be more
debuggable; nothing currently forces new modes through the policy.

### Minor

- `test_mount_builder_filters_overbroad_credentials_by_runtime_agent`
  hardcodes `Path("/tmp/layout")` instead of a tempdir — harmless (no
  filesystem access) but inconsistent with the rest of the file.

## Recommendation

Before archiving the OpenSpec change, either add the missing
chroot-credential and image-layer regression tests (tasks 3.4, 4.3) or reword
those tasks/spec scenarios to reflect what is actually verified. Everything
else is in good shape.
