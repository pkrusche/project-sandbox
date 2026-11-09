# Tasks

## 1. Shared validators

- [ ] 1.1 Extract npm's `local_path()` checks into a shared project-relative path validator. Add the control-character and leading-`-` rules, and keep npm's existing error wording. Verify with unit tests for each rule, and check that the existing `--node-npm` workspace tests in `tests/test_cli.py` still pass.
- [ ] 1.2 Add an OCI image-reference validator and a tag-fragment validator. Verify with table-driven tests:
  - accepted: registry with port, IPv6 domain, digest only, tag and digest, `localhost:5000/img`;
  - rejected: whitespace, newline, uppercase repository path, leading `-`, a tag longer than 128 characters, a bare `:`.

## 2. Argument validation

- [ ] 2.1 Use the validators as `argparse` types for `base_image`, `--image-tag`, `--python` and `--rust`. Verify with CLI tests that invalid values exit with a usage error before any file is written, including with `--dry-run`, and that the default image tag still validates.
- [ ] 2.2 Make `render_python_uv_dockerfile`, `render_rust_cargo_dockerfile` and `dockerfile.render` raise `ValueError` on invalid image or version values. Verify with renderer tests that call them directly.

## 3. Cargo member handling

- [ ] 3.1 Validate members in `_detect_cargo_workspace` with the shared validator. Turn glob escapes and symlinked members that resolve outside the project into named errors. Verify with tests for a newline name, a `..` pattern, a symlink to outside, and a `-rf` directory, each in normal and `--dry-run` mode.
- [ ] 3.2 Render member `COPY` instructions in JSON form, and render the stub step as the fixed exec-form `RUN` from design Decision 1. Verify with renderer tests that pin the script constant, and that assert no member path appears outside a JSON string.
- [ ] 3.3 Add a splicing round-trip test: render a Cargo workspace whose members include `my crate`, `it's` and `crate$(id)`, run it through the splicer, and assert that the final Dockerfile keeps the JSON instructions byte-for-byte. Verify that it passes, extending the splicer's tokenizer if needed.
- [ ] 3.4 Add an opt-in container test (behind `PROJECT_SANDBOX_CONTAINER_TESTS=1`) that builds a Cargo workspace with those member names and checks that the image builds and that a marker file which `$(...)` would create does not exist. Verify on Docker or Podman, and record the result in `docs/development.md`.

## 4. Docs

- [ ] 4.1 Document the Cargo member path rules and the image reference and version formats in `docs/usage.md`. Remove finding #10 from TODO.md. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
