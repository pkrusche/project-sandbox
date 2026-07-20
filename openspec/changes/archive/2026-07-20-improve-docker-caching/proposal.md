## Why

The cache-first `--dockerfile` splice order puts sandbox-owned `apt-get`, `curl`, and `npm` network operations before source Dockerfile instructions that may configure corporate certificates, proxies, or package mirrors. We need an explicit prerequisite stage while retaining cache-stable sandbox dependency layers.

## What Changes

- Render custom Dockerfiles through generated multi-stage inheritance instead of copying all stages through the final `FROM` ahead of sandbox installs.
- Split a Dockerfile without a reserved prefix at its final stage: sandbox dependencies use that stage's original base, and the original final-stage body becomes a postfix stage inheriting the dependencies.
- Recognize a user stage named `prefix`, case-insensitively, as network/build prerequisite setup. Insert the sandbox dependency stage after it and carry those dependencies along the final stage's inheritance path.
- Preserve unrelated build stages, stage names, `FROM` options, parser directives, global arguments, and `COPY --from` references.
- Reject ambiguous or disconnected prefix declarations with actionable errors.
- Document the prefix migration needed by custom Dockerfiles whose setup must precede sandbox-owned network installs.

## Capabilities

### New Capabilities
- `dockerfile-splicing`: Governs dependency-stage insertion, prefix-stage inheritance, final-stage reconstruction, and custom Dockerfile compatibility.

### Modified Capabilities
(none — no main spec currently documents `--dockerfile` splicing behavior)

## Impact

- `src/project_sandbox/dockerfile.py` and `templates/Dockerfile.j2`: stage analysis and inheritance-based rendering replace the current two-fragment splice.
- `tests/test_renderers.py`: regression coverage expands to single-stage, multi-stage, prefix, graph, collision, and syntax-preservation cases.
- `docs/usage.md`: documents the reserved `prefix` authoring contract and migration pattern.
- No CLI flags or public Python APIs change.
