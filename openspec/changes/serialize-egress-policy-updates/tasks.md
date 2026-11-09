# Tasks

## 1. Serialized edits

- [ ] 1.1 Wrap `egress_policy._edit` in `host_locks.path_lock("egress-policy", path)`, with compare-before-replace and up to 3 retries (design Decision 2). Verify with a barrier-controlled test in which two threads both pass the read before either replaces: both `add`s land, and `add` plus `remove` produce the combined result. The test must fail without the lock.
- [ ] 1.2 Simulate a hand edit between read and replace, by monkeypatching the re-read to return changed bytes once, then on every attempt. Verify that the first case re-applies the change onto the new contents, and the second exits non-zero with the "changed while editing" message, leaving the other writer's bytes intact.
- [ ] 1.3 Add `digest` to `PolicyFile`, computed from the file bytes, with a fixed value for a missing file. Verify with unit tests that the digest is stable across loads and changes when an allowlist entry changes.

## 2. Appliers read the current file

- [ ] 2.1 Change `egress_sidecar.ensure`, `reload` and `restart` to take `load_policy` (default `egress_policy.load_global`), call it inside `_locked()`, and return the applied `PolicyFile`. Remove the `PolicyFile` parameter. Verify with tests in which the loader returns a different policy than the caller loaded earlier, and the reconcile uses the loader's result.
- [ ] 2.2 Record `policy_digest` in the sidecar state in `_reconcile` and `_cold_start`. Make an invalid file at apply time raise `SidecarError` without touching the running sidecar. Verify with tests on state contents, and on unchanged rendered config after a failing load.
- [ ] 2.3 Update `egress_cli` (`up`, `restart`, `reload` and the live apply after `allow`) to pass the loader and print "Applied egress.toml <digest>". Make `status` print the running digest and the current digest, marking a mismatch. Verify with `tests/test_egress_cli.py` cases for each command, including `status` for a pre-change state with no digest ("unknown").

## 3. Launch path

- [ ] 3.1 In `cli.py`, keep the early `_egress_sidecar_policy` load for checks and preview, pass the loader to `ensure`, and re-check the Copilot requirement against the applied policy as well as the running sidecar. Verify with CLI tests:
  - a revocation between the early load and `ensure` is not re-admitted (the loader returns the revoked policy);
  - an invalid file at the sidecar step fails before the container starts;
  - Copilot disabled in the file fails even when the running sidecar still has the profile.
- [ ] 3.2 Confirm `--dry-run` still shows the early-loaded effective policy and performs no lock or apply. Verify with the existing dry-run tests, plus an assertion that the loader is never called.

## 4. Docs and cleanup

- [ ] 4.1 Document the concurrency guarantees, the remaining window during hand edits and the digest in `status` in `docs/egress-sidecar.md`. Remove finding #7 from TODO.md. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
