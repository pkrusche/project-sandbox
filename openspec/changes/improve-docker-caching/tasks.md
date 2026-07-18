## 1. Splice-point splitting in dockerfile.py

- [ ] 1.1 Add a helper that splits sanitized source Dockerfile blocks (from `_dockerfile_blocks`) into a "from part" (through the final stage's `FROM`, reusing the same block-walking logic as `_extract_last_from`) and a "rest part" (everything after), returning both as text.
- [ ] 1.2 Update `_read_source_dockerfile()` (or `render()`, whichever is the natural seam) to return/expose both parts instead of a single `sanitized` string, while keeping existing warning generation (base-image, WORKDIR, restricted-user-setup) driven off the same block list as today.
- [ ] 1.3 Update `render()` to pass both `from_part` and `rest_of_source_dockerfile_text` into the template context, replacing the single `source_dockerfile_text` variable.

## 2. Template reorder

- [ ] 2.1 In `templates/Dockerfile.j2`, replace the top `source_dockerfile_text` block with `from_part` (falling back to `FROM {{ base_image }}` when no source Dockerfile was given, as today).
- [ ] 2.2 Move the apt-get base-tooling `RUN`, Node.js install `RUN`, jj install `RUN`, and the `npm install -g` blocks (openspec + per-agent) to run immediately after `ARG`/`USER root`, before any new splice point.
- [ ] 2.3 Add the `{{ rest_of_source_dockerfile_text }}` splice point after the dependency-install blocks.
- [ ] 2.4 Move the `agent` user creation and `/project-sandbox-config`/`/project-sandbox-secrets`/home-directory setup `RUN` blocks into a single trailing block after the rest-of-source splice point, ahead of the firewall/entrypoint `COPY`s, `USER agent`, `WORKDIR`, and `ENTRYPOINT`/`CMD` (whose relative order and position after user content stays as-is).
- [ ] 2.5 Confirm `SHELL ["/bin/bash", "-lc"]` and `USER root` still appear before the dependency installs, matching current behavior for `--dockerfile` builds.

## 3. Tests

- [ ] 3.1 Re-run `tests/test_renderers.py`'s existing `--dockerfile` splicing tests (`test_dockerfile_renderer_extends_source_dockerfile`, `_overwrites_existing_agent_uid_setup`, `_overwrites_existing_jj_install`, `_overwrites_missing_config_mount_targets`, `_removes_source_user_id_setup`) and fix any breakage.
- [ ] 3.2 Add a new test asserting relative order: the template's Node.js (or jj) install line index is less than a user Dockerfile's source-dependent `COPY`/`RUN` line index in the rendered output.
- [ ] 3.3 Add a new test with a multi-stage source Dockerfile (an earlier named `FROM ... AS builder` stage plus a final stage referencing `COPY --from=builder`) asserting both stages remain together ahead of the template's dependency installs, and the `COPY --from=builder` reference is preserved intact in the rest part.
- [ ] 3.4 Add or extend a test confirming the `agent` user/config-directory setup still runs after user content and before `USER agent`/firewall `COPY`s in the rendered output.

## 4. Documentation

- [ ] 4.1 Update `docs/usage.md`'s `--dockerfile` section to describe the new instruction order and what a custom Dockerfile can/cannot assume at each point (apt-get tools like `git`/`curl` now guaranteed present; `agent` user/config paths still not yet created).

## 5. Verification

- [ ] 5.1 Run `uv run python -m compileall src tests` and `uv run pytest -q`.
- [ ] 5.2 Render a `--dockerfile` output for this repo's own root `Dockerfile` (`--dry-run`) and manually inspect that apt-get/Node.js/jj/npm installs precede the `COPY pyproject.toml uv.lock` / `COPY src/` cache-warming layers, and that the `agent` user block still trails the source content.
- [ ] 5.3 Update `TODO.md` to remove the now-implemented "--dockerfile: reorder template splicing" item.
