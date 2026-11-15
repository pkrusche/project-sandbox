# Tasks

Prerequisite: `add-colima-runtime` is archived, so `colima_mounts`, `colima_network` and the `colima-runtime` spec are on the main line.

## 1. Remaining spike questions (throwaway profile, deleted afterwards)

Record each result in `docs/colima-spikes.md` section F with a PASS / WORKAROUND / BLOCKED verdict. A BLOCKED verdict goes back to `design.md`.

- [x] 1.1 Restart behavior: with a stopped profile created by the init command from the design, run `colima start --profile P <full mount list> --activate=false --ssh-config=false` and confirm that CPU, memory and disk are kept from the saved config, the exports are exactly the list, and the active Docker context is unchanged. Verify with the recorded `colima list`, `findmnt` and `docker context show` output.
- [x] 1.2 Symlinked state home: point `XDG_STATE_HOME` at a symlink, export the resolved path and then the unresolved one, and record which a bind mount to the unresolved path needs. Verify with a real `docker run --mount` against each; the verdict confirms or relaxes design D3's refusal.
- [x] 1.3 Running-sandbox detection: start a sandbox container and confirm `docker --context C ps --filter name=project-sandbox-` finds it and excludes the sidecar (`project-sandbox-egress`), and that a profile restart ends it. Verify with the recorded `ps` output before and after.
- [x] 1.4 Credential locations inside the VM: with `~/.ssh` and `~/.aws` outside the exports, confirm a container asked to bind-mount them gets Docker's `bind source path does not exist` and not an empty directory. Verify with `--mount` and with `-v`, which behave differently; record both.

## 2. Pure core (`colima_profile.py`)

- [x] 2.1 Implement `validate_root()` with the refusals from design D4 (not a directory, home or an ancestor, system directory, credential location or a path containing one, inside the state root) and symlink resolution. Verify with a table-driven test of every refusal, its message, and a resolved-symlink acceptance case.
- [x] 2.2 Implement `build_start_argv()` for the exports, `--activate=false`, `--ssh-config=false`, Docker runtime, VZ, virtiofs and the 4 CPU / 8 GiB default with overrides. Verify with argv-level tests: exact exports (and no `$HOME`), the two protective flags always present, quoting of paths with spaces, and each override.
- [x] 2.3 Implement the state-directory symlink refusal (D3) and the creation of the four state subtrees with mode 0700. Verify with tests for a symlinked `XDG_STATE_HOME` (refused, naming the resolved directory) and for directory creation and permissions.
- [x] 2.4 Implement the roots record (D6): write, read and update `<state root>/colima-profiles/<profile>.json` with mode 0600, never under an exported path. Verify with tests for round trip, permissions, update on `add-root`, and that its directory is not in `required_state_dirs()`.
- [x] 2.5 Implement the Colima flag check (D8) from `colima start --help`. Verify with tests for a help text that has every flag, and for each flag missing in turn (the error names it).

## 3. `colima init`

- [x] 3.1 Add the `colima` subcommand dispatch in `cli.py` and `colima_profile.main()` with `init`, `--workspace-root`, `--colima-profile`, `--cpu`, `--memory`, `--disk`, `--yes` and `--dry-run`. Verify with tests that `--help` lists them, that a missing `--workspace-root` exits before anything runs, and that the positional CLI is unchanged.
- [x] 3.2 Implement init: refuse an existing profile in any state, print the command, confirm, create the state subtrees, run `colima start` with the terminal attached, write the record. Verify with fake-runner tests: existing profile refused with no mutating call; `--dry-run` runs nothing and creates no directory; declined confirmation runs nothing and exits non-zero; no TTY without `--yes` is an error; success writes the record.
- [x] 3.3 Document init in `docs/colima.md` (what is exported, the flags, the roots rules, moving from `default`) and `docs/doctor.md`. Verify that every command shown is run as written against a throwaway profile, with the output recorded in `docs/colima-spikes.md`.

## 4. `colima add-root`

- [x] 4.1 Implement add-root: read the exports (refuse when unreadable), validate the root, refuse when already exported, build the full list with `mount_flags`, confirm, stop and start, update the record. Verify with fake-runner tests that every current export and its access mode is in the restart command and the new path is never alone, plus the unreadable and already-exported cases.
- [x] 4.2 Implement the running-sandbox guard and `--force`. Verify with tests for a running container (refused, naming it and `--force`), the sidecar alone (not counted), and `--force`.
- [x] 4.3 Document add-root, and replace the hand-typed `--mount` advice in the launch message and in `doctor`'s hint with it when the profile has a record. Verify with tests of both messages and a rendered-docs check that no document still suggests a bare `colima start --mount X`.

## 5. Doctor

- [x] 5.1 Warn when a profile exports `$HOME` or an ancestor read-write, naming `colima init`. Verify with tests for a home export, an ancestor export, a read-only home export (not a warning), and a dedicated profile (no warning).
- [x] 5.2 For a profile with a record, report exports that are not roots or state subtrees, and roots that are not exported. Verify with tests for exact, drifted (extra export named), and missing (named) cases, and for a hand-made profile without a record (only the home check).
- [x] 5.3 Keep every `doctor` probe read-only. Verify with the existing read-only argv test extended to the new probes.

## 6. End to end and release

- [x] 6.1 Add an opt-in `hardened` check to `scripts/e2e-colima.py`: `init` a throwaway profile, run the launch and recover checks against it, assert the VM cannot see `~/.ssh`, `~/.aws` or the session records, then delete the profile and confirm the active Docker context and the `default` profile are unchanged. Verify by running it and recording the result; the grader functions are tested without a runtime.
- [x] 6.2 Add the CHANGELOG entry and the `docs/development.md` section for the check. Verify the documented commands as written.
- [x] 6.3 Run `uv run python -m compileall src tests`, `uv run ruff check src tests`, `uv run ty check`, `uv run pytest -q`, and `openspec validate add-colima-hardened-profile --strict`. Verify that all pass.

## Workflow follow-up

- Archive the change after review and after the `hardened` end-to-end check passes.
