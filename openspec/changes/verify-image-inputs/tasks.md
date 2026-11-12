# Tasks

## 1. Labels

- [ ] 1.1 Capture `image inspect` output with labels on Docker, Podman and Apple `container`, and add a per-runtime label parser with fixture tests. Verify the fixtures round-trip both labels.
- [ ] 1.2 Compute `inputs` over the whole generated context and `settings` (design Decision 1), and pass both labels on every build. Verify with tests that a change to any generated file changes `inputs`, that the tag doesn't, and that a credentialed build proxy is redacted in `settings`.

## 2. Reuse and verification

- [ ] 2.1 Replace the `.build-state.json` check with label-based reuse, keeping the identity check and session alias. Verify with CLI tests for unchanged inputs (no build), an out-of-band replaced tag (rebuild), and whole-project flows (always build, labeled).
- [ ] 2.2 Add `--no-build` verification with the settings diff (Decision 2). Verify with CLI tests for a base-image mismatch, an unlabeled image, a template-only change and a match.
- [ ] 2.3 Add the `--build-only` warnings (Decision 3). Verify with a CLI test.

## 3. Docs

- [ ] 3.1 Document labels, reuse and `--no-build` verification in `docs/usage.md` (Image builds) and `docs/runtime.md` (Concurrent sessions), and the one-time rebuild in the CHANGELOG. Verify that `uv run pytest -q`, `uv run ruff check src scripts tests` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- Archive with `/opsx:archive`, creating `openspec/specs/image-identity/`.
