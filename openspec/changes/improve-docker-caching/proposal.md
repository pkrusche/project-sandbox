## Why

When `--dockerfile` is used, `dockerfile.render()` splices the user's entire sanitized source Dockerfile in as a prefix, and only *after* it appends the template's own base-image-only dependency installs (`apt-get`, Node.js, jj, per-agent `npm install -g`) plus the `agent` user/firewall setup. Docker's build cache invalidates every layer after the first one that changed, so any source-dependent step in the user's Dockerfile (typically a `COPY src/` for cache-warming a language toolchain) sits *before* those expensive installs — an ordinary source edit forces Node.js/jj/all agent CLIs to reinstall on every rebuild even though none of them depend on user source at all. This was found while making this repo's own root `Dockerfile` cache-friendly: splitting its `uv sync` step into a deps-only layer and a project-install layer only bounds the small `uv sync` cost — it does nothing for the much larger reinstall cost imposed by the template's own splice order.

## What Changes

- Reorder the merged Dockerfile emitted for `--dockerfile` builds so cache-stable, base-image-only installs (`apt-get`, Node.js, jj, per-agent npm installs) run immediately after `FROM`/`ARG`/`USER root`, *before* any user-supplied, source-dependent content.
- Split `dockerfile.render()`'s handling of the sanitized source Dockerfile text into a "from part" (up through the final stage's `FROM`, preserving any earlier named multi-stage build stages) and a "rest part" (everything after), spliced at two different points in the template.
- Move the `agent` user/firewall/config-directory setup that today is interleaved with the dependency installs into its own trailing block, after the "rest part" splice point, so its position relative to user content is unchanged while its position relative to the dependency installs moves.
- Update `docs/usage.md`'s description of the `--dockerfile` flow to document the new splice order and what a custom Dockerfile can and cannot assume is present at each point (gains apt-get-installed tools like `git`/`curl` for free; still cannot assume the `agent` user exists yet).

## Capabilities

### New Capabilities
- `dockerfile-splicing`: Governs how a user-supplied `--dockerfile` is merged with project-sandbox's own template content (dependency installs, agent user/firewall setup) into the final rendered Dockerfile, including instruction ordering and multi-stage handling.

### Modified Capabilities
(none — no existing spec currently documents `--dockerfile` splicing behavior)

## Impact

- `src/project_sandbox/dockerfile.py`: `render()` and `_read_source_dockerfile()`/`_extract_last_from()`-adjacent helpers need to produce a "from part" and "rest part" split of the sanitized source text instead of a single blob.
- `src/project_sandbox/templates/Dockerfile.j2`: reordered so dependency-install `RUN` blocks come right after `FROM`/`ARG`/`USER root`, followed by a new splice point for the rest of the user's Dockerfile, followed by the `agent` user/firewall/`WORKDIR`/`ENTRYPOINT` block.
- `docs/usage.md`: `--dockerfile` section needs updated ordering guidance.
- `tests/test_renderers.py`: existing `--dockerfile` splicing tests (`test_dockerfile_renderer_extends_source_dockerfile`, `_overwrites_existing_agent_uid_setup`, `_overwrites_existing_jj_install`, `_overwrites_missing_config_mount_targets`, `_removes_source_user_id_setup`) assert content via `assertIn`/`assertNotIn` rather than relative order, so should keep passing but must be re-run to confirm.
- No change needed to `render_python_uv_dockerfile`/`render_rust_cargo_dockerfile` — those generate standalone Dockerfiles and never go through `Dockerfile.j2` splicing.
