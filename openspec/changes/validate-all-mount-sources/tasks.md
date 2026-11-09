# Tasks

## 1. Mount model, parser and encoder

- [ ] 1.1 Replace `MountSpec`'s `raw` with the tagged `BindMount`/`TmpfsMount`/`VolumeMount` model, and a strict key/value parser that refuses duplicate, unknown and unsupported keys and flags (design Decision 1). Verify with parser tests for every supported form, each rejected option (`bind-propagation=rshared`, `z`, `volume-opt`, unknown type), duplicate keys and invalid volume names.
- [ ] 1.2 Add the refusing encoder (design Decision 4), and use it for Docker, Podman and Apple argv and for `devcontainer.json`. Verify with a property-based round-trip test (`parse(encode(m)) == m`), and with refusal tests for `,`, `"`, a newline and `=` in paths.
- [ ] 1.3 Build every implicit mount in `cli.py` and `devcontainer.py` as a structured value, never a string. Switch `_mount_touches_path` to structured comparison. Verify that the existing mount argv tests pass unchanged for ordinary paths, and add a test where the project path contains a comma (refused before any container starts).

## 2. Source rules

- [ ] 2.1 Make `check_location` check both directions, and apply the protected-location check (policy directory, egress state, state root, runtime root) to every bind source in every egress mode. Verify with tests for:
  - a subdirectory of the egress state (refused in firewall and sidecar mode);
  - `$HOME` containing the state root (refused);
  - the policy directory with an extra option (refused at parse time);
  - an unrelated directory (accepted).
- [ ] 2.2 Add the workspace-containment rule, and pass strictly resolved sources to the runtime. Verify with tests for a planted symlink under the project (refused) and a regular project subdirectory (accepted, resolved path in argv).
- [ ] 2.3 Add credential-directory warnings, and refuse volumes in sidecar mode while warning about them in firewall mode. Verify with CLI output tests for each case.

## 3. Target rules and ordering

- [ ] 3.1 Add target validation (protected prefixes, `/workspace` itself, normalization), and assert the mount order from design Decision 3. Verify with tests for `/usr/local/bin` (refused), `/opt/cache` (accepted), `/workspace` (refused), `/workspace/data` (accepted), and a user mount on `/workspace/.devcontainer` that is still masked last in argv.

## 4. Docs

- [ ] 4.1 Document the supported `--mount` forms, the source and target rules, protected locations, credential warnings and volume handling in `docs/usage.md` and `docs/security.md`. Remove finding #5 and the "Mount encoding" hardening item from TODO.md. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
