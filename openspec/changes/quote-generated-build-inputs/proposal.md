# Proposal

## Why

`--rust-cargo` reads workspace member paths from the repository's `Cargo.toml` and pastes them unquoted into the generated Dockerfile:
- space-delimited `COPY` instructions;
- `mkdir`, `touch` and `rm -rf` shell commands (`dockerfile.py:1017–1034`).

A member directory named `crate$(id)` renders as `RUN mkdir -p crate$(id)/src`, so a repository file decides what the image build runs as root. Benign names with spaces break the build. A leading `-` turns a path into an option. This is security finding #10 in TODO.md.

The same review asks for validation of image and version strings before they reach `FROM`. `base_image`, `--python` and `--rust` are pasted into `FROM` lines as they are. They are CLI arguments, but they can come from a repository-committed `.project-sandbox-tui.toml` (`base_image`, `build_version`). A newline in one of them adds Dockerfile instructions.

`--node-npm` already encodes member paths as JSON `COPY` arrays and confines them to the project. This change brings the other generated flows to the same standard.

## What Changes

- **Cargo member paths are data, never syntax.**
  - Member `COPY` instructions use the JSON array form.
  - The stub create/remove step passes member paths as arguments to a fixed exec-form script. They are never interpolated into shell text.
- **Cargo members are validated before rendering.** The build is refused with an error naming the member and the reason when a member path:
  - contains control characters or a newline;
  - is absolute or contains `..`;
  - resolves outside the project, for example through a symlinked directory;
  - starts with `-`.
- **Image references and version tags are validated before rendering.**
  - `base_image` and `--image-tag` must be valid OCI image references.
  - `--python` and `--rust` must be valid image tag fragments that still produce a valid reference (`python:<v>-slim`, `rust:<v>-slim`).
  - Invalid values fail at argument validation, before any file is written. This also applies to `--dry-run`.
- **BREAKING (edge cases only):** projects whose Cargo members, or whose image or version values, fall outside these rules stop building. Such builds were already broken or unsafe.

## Capabilities

### New Capabilities

- `dockerfile-input-encoding`: how values from the repository or the command line are validated and encoded when they are rendered into generated Dockerfiles and their build-time shell steps. This covers Cargo members, npm members (already compliant), image references and version tags.

### Modified Capabilities

(none. `node-npm-flavor` already requires member paths that stay inside the project, and its renderer already uses JSON `COPY`. The new capability's encoding rules cover npm members too, without changing npm behavior, except that control characters in member paths are now rejected. `dockerfile-splicing` treats user Dockerfiles as trusted input and is unchanged.)

## Impact

- **Code:**
  - `dockerfile.py`: `render_rust_cargo_dockerfile` and the `FROM` construction in `render_python_uv_dockerfile`, `render_rust_cargo_dockerfile` and the base-image dependency stage.
  - `cli.py`: `_detect_cargo_workspace` validation, and argument validation for `base_image`, `--image-tag`, `--python` and `--rust`.
- **Generated files:** `Dockerfile.rust-cargo` changes shape: JSON `COPY` and an exec-form stub step. The fingerprint changes, so images rebuild once.
- **Out of scope:**
  - User-supplied `--dockerfile` content stays trusted, image-producing code.
  - Project build scripts (`build.rs`, `uv sync` build backends, npm lifecycle scripts) still run at build time by design.
- **Docs:** the `--rust-cargo` section of `docs/` gains the member path rules. `docs/security.md` drops the quoting caveat.
