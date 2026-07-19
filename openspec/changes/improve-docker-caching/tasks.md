## 1. Stage analysis

- [x] 1.1 Parse source Dockerfile blocks into stages while preserving leading directives/comments, complete `FROM` text, options, base tokens, aliases, and instruction bodies.
- [x] 1.2 Select a deterministic collision-free dependency-stage name and detect duplicate case-insensitive `prefix` declarations.
- [x] 1.3 Build named-stage inheritance links, validate that prefix reaches the final stage, and identify the single edge to rewrite on that path.
- [x] 1.4 Resolve the effective external base image through aliases for compatibility warnings.

## 2. Inheritance rendering

- [x] 2.1 Replace final-stage text splitting with rendering fragments for source-before-dependencies, dependency `FROM`, and inherited source-after-dependencies.
- [x] 2.2 Render no-prefix single- and multi-stage sources by splitting the final stage into dependency and postfix stages.
- [x] 2.3 Render prefix sources by inserting after prefix and rewriting only the final ancestry path; handle a final prefix stage.
- [x] 2.4 Update the template so dependency installs live in the generated stage and agent/firewall setup remains in the inherited final stage.

## 3. Tests

- [x] 3.1 Cover single-stage and no-prefix multi-stage rendering, aliases, `COPY --from`, global `ARG`, and `FROM` options.
- [x] 3.2 Cover case-insensitive prefix setup, transitive inheritance, unrelated branches, and a final prefix stage.
- [x] 3.3 Cover duplicate/disconnected prefixes and deterministic internal-name collisions.
- [x] 3.4 Re-run existing sanitization, warning, ordering, and renderer regression tests for both generated Dockerfiles.

## 4. Documentation

- [x] 4.1 Document the inheritance model, prefix migration example, validation rules, and the point at which agent setup becomes available.

## 5. Verification

- [x] 5.1 Validate the OpenSpec change and run `uv run python -m compileall src tests`.
- [x] 5.2 Run focused renderer tests and `uv run pytest -q`.
- [x] 5.3 Inspect a dry-run/render of the repository Dockerfile to confirm dependency, postfix, and agent setup ordering.

## 6. Code-review follow-ups

- [x] 6.1 Re-emit `ARG AGENT_UID` / `ARG AGENT_GID` in the template after the spliced postfix content, before agent setup — ARGs do not cross the new `FROM` boundary, so every `--dockerfile` render currently fails `docker build` with `useradd -m -u ""`. Add a regression test asserting the redeclaration appears after the final spliced `FROM`.
- [x] 6.2 Tighten `_NUMERIC_STAGE_REFERENCE_RE` to match only real `--from=<N>` / `--mount=...,from=<N>` stage options and skip comment blocks; regression-test that `RUN echo "from=2 backup"` and `# migrated from=3` no longer abort rendering.
- [x] 6.3 Run the prefix-ancestor validation before `_reject_shifted_stage_references` so a disconnected prefix reports the actionable "not an ancestor" error; test the error precedence.
- [x] 6.4 Strip line-continuation backslashes when parsing/rewriting `FROM` blocks (reuse the shared flattening helper) so a multi-line `FROM ... \` + `AS <alias>` keeps its alias; regression-test alias preservation.
- [x] 6.5 Make `_dockerfile_blocks` heredoc-aware: when an instruction carries a `<<EOF`-style redirection, treat every line up to the matching terminator as part of that block's opaque body, so an embedded `FROM` line cannot become a stage boundary or the dependency stage's base. Regression-test a `COPY <<EOF` whose body contains `FROM scratch`, and a benign heredoc without `FROM` lines.
- [x] 6.6 Resolve single-assignment global `ARG` defaults when matching `FROM` base tokens against stage names, so `ARG BASE=prefix` + `FROM $BASE AS final` links the stage graph (support `$NAME` and `${NAME}` forms; leave unresolvable or reassigned ARGs unmatched). Extend the "not an ancestor" error to mention variable-expanded `FROM` references that could not be resolved. Regression-test the `ARG BASE=prefix` chain and the still-unresolvable case.
- [x] 6.7 Cleanup: remove the dead `_first_non_option_token`, extract the repeated block-flattening idiom into one helper, unify generated-`FROM` emission (`_generated_from` vs `_rewrite_from_block`), and thread parsed blocks/stages from `_read_source_dockerfile` so each render parses the source once.
