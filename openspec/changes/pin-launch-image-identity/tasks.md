# Tasks

## 1. Runtime probes

- [ ] 1.1 On Docker, Podman and Apple `container` 1.7.1:
  - build a tiny image with two `--label`s;
  - capture `image inspect` output;
  - add and remove a second tag;
  - confirm the image survives removal of one of two tags.

  Store the inspect outputs as test fixtures under `tests/fixtures/`. Record the untag command per runtime in design.md Decision 2. Verify that the label parser tests pass on all three fixtures.

## 2. Fingerprint and labels

- [ ] 2.1 Replace `build_cache` with a fingerprint over every file in the session `build/` directory plus canonical `settings` and `host_identity`, and remove `.build-state.json` reading and writing. Verify with tests that:
  - a changed CA file, ignore file or Dockerfile changes the fingerprint;
  - identical inputs give identical fingerprints;
  - the file order on disk doesn't matter.
- [ ] 2.2 Build the `settings` summary (design Decision 1) with a no-secrets assertion. Verify with tests that agent-proxy keys, proxy credentials and tokens never appear. Add a test that enumerates options affecting rendered build files, and asserts each one is represented.
- [ ] 2.3 Pass both labels on every build, and read them back through `image inspect` per runtime. Verify with argv tests and parser tests on the task 1.1 fixtures.

## 3. Cache validity and session tags

- [ ] 3.1 Key the build lock by runtime and image repository, and make the cache-hit decision label-based (`inputs` equals the fingerprint) under it. Verify with tests for:
  - two projects sharing an `--image-tag` (their builds are serialized);
  - an unchanged image (skipped);
  - a missing label (built);
  - a tag replaced out of band with a different label (built).
- [ ] 3.2 Add the session tag inside the build lock, launch from it, and remove it in the teardown `finally`. Verify with a barrier-controlled test: session A releases the lock, B rebuilds the tag with a different base image, and A's `run` argv still references A's session tag, which points at A's image. Add argv tests for teardown on success, failure and interrupt.
- [ ] 3.3 Reap session tags of dead sessions at launch, using the `private-session-state` leases. Verify with a test that a `SIGKILL`ed session's tag is removed and a live one's is kept.

## 4. `--no-build` and `--build-only`

- [ ] 4.1 Make `--no-build` render inputs, compare them with the `inputs` label, and print the `settings` diff on mismatch. Verify with CLI tests for an image built with another base image (refused, naming `base_image`), an unlabeled image (refused) and a matching image (launches without building).
- [ ] 4.2 Make `--build-only` warn about options that don't affect the image. Verify with CLI tests for `--build-only --pi-ollama` (warning, build proceeds) and plain `--build-only` (no warning).

## 5. Docs

- [ ] 5.1 Rewrite `docs/usage.md` "Image Builds": labels, label-based caching, session tags, and `--no-build` verification with an example mismatch message. Remove finding #6 from TODO.md. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
