## Context

`project-sandbox --dockerfile <path>` merges a user-supplied Dockerfile with project-sandbox's own template (`templates/Dockerfile.j2`) so the resulting image gets the project's dependency toolchain *and* the sandbox's agent CLIs, firewall, and unprivileged `agent` user. Today `dockerfile.render()` reads and sanitizes the source Dockerfile (`_read_source_dockerfile`, which strips restricted `USER`/`useradd`-family instructions) and passes the whole sanitized text into the template as a single `source_dockerfile_text` block, rendered first. The template then appends, in order: `apt-get` base tooling, the `agent` user creation, config directories, Node.js, jj, and per-agent `npm install -g` calls, then firewall/entrypoint `COPY`s and `USER agent`.

Docker (BuildKit) layer caching invalidates every layer from the first changed instruction onward. Because the user's entire Dockerfile — including any source-dependent `COPY`/`RUN` cache-warming — is emitted before the template's dependency installs, an ordinary source edit invalidates not just the user's own layers but also the Node.js download/verify, jj download/verify, and every `npm install -g` step that follows, even though none of those depend on user source. This was discovered concretely in this repo's own root `Dockerfile`, where the source Dockerfile passed to `--dockerfile` already splits its `uv sync` into a deps-only layer and a project-install layer — an internal split that only bounds `uv sync`'s own cost and cannot help the Node.js/jj/npm cost that lives entirely in the template, after the whole user fragment.

## Goals / Non-Goals

**Goals:**
- Reorder the rendered Dockerfile so template-owned, base-image-only installs (apt-get, Node.js, jj, npm agent installs) sit immediately after `FROM`/`ARG`/`USER root`, before any user-supplied content, so they cache-hit regardless of source edits.
- Preserve today's semantics for what the user's Dockerfile can rely on: it still runs as `root`, still comes before the `agent` user/firewall/entrypoint setup, and multi-stage source Dockerfiles are still handled correctly (only the final stage's tail is treated as "user content" to splice after the installs; earlier named stages stay together with the final `FROM`).
- Keep the sanitization behavior (`_remove_restricted_user_setup`, base-image warnings, `WORKDIR` warnings) unchanged — only the splice *points* change, not what gets removed or warned about.

**Non-Goals:**
- Changing `render_python_uv_dockerfile` / `render_rust_cargo_dockerfile` (the `--python-uv` / `--rust-cargo` generated Dockerfiles). These write a standalone Dockerfile directly and never go through `Dockerfile.j2` splicing, so they're unaffected.
- Introducing multi-stage builds into the template itself, or caching `/opt/uv-cache`-style artifacts across builds — this change is purely about instruction *order* within the single rendered stage.
- Changing what the template installs or which base images are supported.

## Decisions

**Split point: last top-level `FROM`, reusing `_extract_last_from`'s block-walking.** `dockerfile.render()` already needs to find the effective base image for warnings via `_extract_last_from(blocks)`, which walks `_dockerfile_blocks(text)` and records the last top-level `FROM`. Reuse the same block list to partition the sanitized text into a "from part" (all blocks up to and including the final stage's `FROM`, so any earlier named stages in a multi-stage user Dockerfile stay intact and available to later `COPY --from=`) and a "rest part" (every block after that `FROM`). Rejected alternative: re-parsing with a separate regex pass — reusing the existing block list avoids duplicating the multi-stage-aware logic and keeps the two code paths from drifting apart.

**Two splice points in the template, not one.** `Dockerfile.j2` gets `{{ from_part }}` at the top (replacing today's single `source_dockerfile_text` block there), then the dependency-install blocks (apt-get, Node.js, jj, npm) immediately after `ARG`/`USER root`, then a new `{{ rest_of_source_dockerfile_text }}` splice, then the `agent` user/config-dir/firewall/`WORKDIR`/`ENTRYPOINT` block. Rejected alternative: keeping a single splice point and instead moving the *installs* before it via Jinja conditionals on both sides — considered, but two splice points map directly to the two logical halves of the source text and are simpler to reason about than duplicating conditional blocks.

**`agent` user setup moves to a trailing block, not interleaved.** Today the `agent`-user `RUN` sits between the apt-get install and the Node.js install (both cache-stable, base-image-only steps). Moving `useradd`/config-dir creation into its own block *after* the rest-of-source splice keeps its position relative to user content unchanged (still after) while removing it from between the two cache-stable install steps, so nothing forces those install steps to re-run because of unrelated user-setup logic changes. Considered keeping it in place: rejected because leaving it interleaved would still work for cache purposes, but only by coincidence (it depends on nothing above or below it) — moving it to a single trailing block makes the four-part structure (from → installs → user content → agent/firewall setup) explicit and easier to reason about than an accidental ordering.

**No caching help for `render_python_uv_dockerfile`/`render_rust_cargo_dockerfile`.** These don't go through `Dockerfile.j2` at all — confirmed by reading `dockerfile.py`, they write a standalone Dockerfile string directly. Nothing to change there.

## Risks / Trade-offs

- **[Risk]** A user's Dockerfile install step assumed something from the old ordering (e.g., relied on `apt-get`-installed `git`/`curl` being present at a specific point, or relied on running before some now-earlier step) → **Mitigation**: the new order is strictly *more* permissive for user content (apt-get tools are now guaranteed present before user content runs, where before they weren't), and the only thing that moves *later* relative to user content is the `agent`/firewall setup, which already ran after user content today. No previously-guaranteed ordering becomes unavailable. Document the new guarantees in `docs/usage.md`.
- **[Risk]** Multi-stage user Dockerfiles could have the split point land in the wrong place, separating a named stage from a later `COPY --from=<stage>` → **Mitigation**: reuse `_extract_last_from`'s existing block-walking so the from-part boundary is anchored to the exact same "final stage" definition already relied on for the base-image warning; add a regression test with a multi-stage source Dockerfile asserting all named stages stay in the from-part.
- **[Risk]** Existing splicing tests in `tests/test_renderers.py` assert via `assertIn`/`assertNotIn` and could pass without actually verifying the new order → **Mitigation**: explicitly re-run them per the proposal's Impact section, and add at least one new test asserting relative order (e.g., that the Node.js install line index is less than a user `COPY src/` line index) since none of the existing tests check order today.

## Migration Plan

No data migration; this only changes generated Dockerfile content for future `--dockerfile` runs. Existing built images are unaffected until rebuilt. No flag or opt-out is introduced — the new order is strictly a superset of guarantees, so no backward-compatibility path is needed. Roll out as a normal code change; if unforeseen breakage surfaces, revert the template/render change (no persistent state to unwind).

## Open Questions

None outstanding — the approach was already validated in `TODO.md` prior to this proposal.
