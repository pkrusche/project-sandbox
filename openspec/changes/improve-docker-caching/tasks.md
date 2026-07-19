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
