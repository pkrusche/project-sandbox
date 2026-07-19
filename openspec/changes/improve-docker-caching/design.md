## Context

The current implementation splits sanitized source text after its final `FROM`, emits all earlier stages and that final `FROM`, then runs sandbox-owned dependency installs before the final-stage body. This improves caching but assumes the final base is already ready for network access. A previous source Dockerfile could instead install a corporate CA, configure an apt mirror, or set proxy state before those operations.

Docker only carries filesystem and image configuration between stages when a later `FROM` inherits an earlier stage. Textual placement of an unrelated named stage is not enough, so the generated Dockerfile must model the cache boundary with explicit stage inheritance.

## Goals / Non-Goals

**Goals:**
- Keep sandbox dependency installs ahead of source-dependent final-stage instructions.
- Allow source-defined network prerequisites to run first through a case-insensitive `prefix` stage.
- Preserve valid single-stage and multi-stage source semantics, including unrelated build branches.
- Keep sanitization, warnings, agent setup, and generated-image behavior unchanged except where stage-aware analysis is required.

**Non-Goals:**
- Inferring which arbitrary source instructions are network prerequisites.
- Transplanting instruction bodies between unrelated base images.
- Changing generated `--python-uv` or `--rust-cargo` Dockerfiles.

## Decisions

**Always create an explicit dependency stage for custom Dockerfiles.** Without `prefix`, split the source's final stage. Reuse its complete `FROM` base/options for a uniquely named dependency stage, then emit `FROM <dependency-stage>` with the original final-stage alias before its body. Earlier stages remain unchanged.

**Treat `AS prefix` as an inheritance marker.** Match the declared name case-insensitively while preserving its spelling. Emit `FROM <actual-prefix-name> AS <dependency-stage>` after the complete prefix stage. Build the later stage inheritance graph, find the unique ancestor path from the final source stage to prefix, and rewrite only the first `FROM prefix` edge on that path to inherit the dependency stage. Other branches and `COPY --from=prefix` references remain unchanged. If prefix is itself final, add a generated final stage inheriting the dependency stage.

**Validate the graph rather than guessing.** Reject multiple case-insensitive prefix declarations and a prefix that is not an ancestor of the final stage. Stage inheritance references resolve only to previously declared named stages. This prevents a declared prerequisite from silently having no effect.

**Generate a collision-free internal name.** Start with `project-sandbox-dependencies` and add the lowest numeric suffix not used by a source stage, comparing names case-insensitively. This avoids reserving a new invalid user namespace.

**Keep dependency and final setup separate in the template.** The analyzer supplies source text before the dependency stage, the dependency `FROM`, and source text after dependency installs. The template emits sandbox dependency instructions in the generated stage, followed by inherited postfix stages and then agent/firewall setup in the final stage.

**Redeclare agent build arguments in the inherited final stage.** Docker `ARG` scope ends at each `FROM`, so `ARG AGENT_UID`/`ARG AGENT_GID` declared in the dependency stage are empty in the postfix/final stage where the agent user is created — `--build-arg` values only reach stages that redeclare the `ARG`. The template must re-emit both `ARG` declarations after the spliced postfix content, immediately before the agent setup instructions. A regression test asserts the redeclaration appears after the final spliced `FROM`, since text-ordering tests alone green-lit the unbuildable render.

**Anchor the numeric-reference guard to real stage options and order validation by actionability.** The shifted-index rejection must match only actual `--from=<N>` / `--mount=...,from=<N>` stage options — not any `from=<digits>` substring in `RUN` strings, URLs, or comment blocks. Prefix-ancestor validation runs before the numeric-reference guard so a disconnected prefix reports the ancestor error (the actionable fix) rather than a misleading shifted-index error.

**Parse `FROM` blocks through the shared continuation-aware flattening.** Joining a multi-line `FROM ... \` continuation without stripping trailing backslashes makes the alias parse as `\` and silently drops it from the rewritten stage, breaking later `--from=<alias>` references. `FROM` parsing and rewriting use the same block-flattening helper as the sanitizer (single implementation, not a fifth inline copy), parse each source once per render (thread blocks/stages from `_read_source_dockerfile` instead of re-tokenizing), and emit generated `FROM` lines through one routine.

**Resolve warnings through stage aliases.** Determine the external image underlying the selected prefix/final base when checking apt compatibility. Continue running restricted-user and WORKDIR/local-install analysis over the source instructions.

## Risks / Trade-offs

- **Existing prerequisite Dockerfiles require migration** → Document the `AS prefix` / inherited-final pattern and fail malformed declarations clearly.
- **A lightweight parser cannot support every Dockerfile frontend extension** → Preserve instruction text verbatim and parse only top-level `FROM` structure, options, image token, and optional alias.
- **Heredoc bodies defeat line-based stage parsing** → A `RUN <<EOF`/`COPY <<EOF` body line beginning with `FROM` is misread as a stage boundary, so the dependency stage can be spliced into the middle of a heredoc (e.g. `FROM scratch AS project-sandbox-dependencies`). Decision: make the block parser heredoc-aware — when an instruction carries a heredoc redirection, absorb every line up to the matching terminator into that block's opaque body. Benign heredocs keep rendering; embedded `FROM`-like lines can no longer corrupt the stage graph.
- **ARG-expanded stage references are invisible to the string-matched graph** → `ARG BASE=prefix` + `FROM $BASE AS final` builds fine under BuildKit but the analyzer sees no `prefix` parent and hard-fails with "not an ancestor". Decision: resolve single-assignment global `ARG` defaults (`$NAME` / `${NAME}`) when matching `FROM` base tokens against stage names; leave unresolvable or reassigned ARGs unmatched and extend the "not an ancestor" error to name unresolved variable-expanded references as a possible cause.
- **Rewriting the wrong branch could alter a build** → Rewrite only the direct prefix edge on the final stage's unique inheritance chain; leave unrelated stages untouched.
- **Generated stage names can collide** → Select a deterministic unused name.

## Migration Plan

Custom Dockerfiles that need network setup before sandbox installs add a `prefix` stage and make their final inheritance chain descend from it. Dockerfiles without such prerequisites require no source change. Existing images remain valid until rebuilt; rollback is a normal code revert.

## Open Questions

None.
