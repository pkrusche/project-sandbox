# Design

## Context

- `_detect_cargo_workspace` (`cli.py:2614`) expands `[workspace].members` globs with `project.glob()`. It keeps every match that has a `Cargo.toml` and returns `relative_to(project).as_posix()`, with no other checks. A pattern that escapes the project raises an unhandled `ValueError`. A symlinked member directory is followed silently.
- `render_rust_cargo_dockerfile` (`dockerfile.py:1006–1034`) emits:
  - `COPY {member}/Cargo.toml {member}/`;
  - one shell `RUN` that runs `mkdir -p`, `touch` and `rm -rf` over the members, joined with spaces.
- The generated Rust Dockerfile is not built directly. It goes through the same splicing as a user `--dockerfile` (`dockerfile-splicing`), which parses its instructions and inserts sandbox stages. The splicer also adds a `SHELL` instruction, which is why the renderer restores `PATH` in `/etc/profile.d`.
- `FROM` lines are built by string formatting:
  - `FROM python:{python_version}-slim` (`dockerfile.py:888`);
  - `FROM rust:{rust_version}-slim` (`:969`);
  - `FROM {base_image}` (`:189`).

  None of these values is validated. `--image-tag` only reaches argv (`build -t`), but a bad value there gives a confusing runtime error rather than a clear one.
- The npm renderer is the model to follow: `COPY` in JSON form, and paths confined to the project by `local_path()` (`cli.py:2693`).

## Goals / Non-Goals

**Goals:**
- No repository-derived or CLI-derived string is ever parsed as Dockerfile or shell syntax in generated recipes.
- Validation fails early, with an actionable message, before anything is written.

**Non-Goals:**
- Sandboxing build scripts (`build.rs`, npm lifecycle scripts, Python build backends). They are trusted image-producing code, and `docs/security.md` already says so.
- Validating user `--dockerfile` content.
- A general Dockerfile escaping library. Only the instructions the generators emit need encoding.

## Decisions

### 1. Pass member paths as exec-form arguments, not quoted shell text

The stub step becomes one exec-form `RUN` with a fixed script. The paths are positional arguments, serialized with `json.dumps`:

```dockerfile
RUN ["/bin/sh", "-c", "set -eu; for d do mkdir -p -- \"$d/src\"; : > \"$d/src/lib.rs\"; done; cargo fetch --locked; for d do rm -rf -- \"$d/src\"; done", "stub", ".", "crates/my crate", "crate$(id)"]
```

- The root crate, when it needs a stub, is passed as `.`.
- The script text is a constant, so tests can pin it exactly.
- The exec form bypasses the splicer's `SHELL`. `cargo` is still found, because the official `rust` image sets `PATH` through `ENV`, which exec form inherits.

**Alternatives considered:**
- *`shlex.quote` each path inside the existing shell `RUN`.* This is correct, but it leaves quoting rules in two places: Dockerfile line continuation and POSIX sh. Every future edit to the step has to keep both right. Positional arguments avoid quoting entirely.
- *Generate stub files in the build context and `COPY` them.* This avoids the shell for creation, but removal still needs a shell step. It also puts more generated files in the build context, which `private-session-state` is about to move.

### 2. `COPY` uses the JSON form

`COPY ["crates/my crate/Cargo.toml", "crates/my crate/"]`, produced by `json.dumps`, the same as npm. The trailing `/` on the destination keeps directory semantics.

### 3. Validate Cargo members against the same rules as npm members

Extract the checks in npm's `local_path()` into a shared validator for project-relative paths, used by both flows. It checks, in order:

1. no control characters (`\x00–\x1f`, `\x7f`);
2. not absolute, and no `..` component;
3. no leading `-`;
4. `(project / path / "Cargo.toml").resolve()` stays under `project.resolve()`;
5. not `.` (the root is handled separately).

A glob that escapes the project becomes this validation error instead of a traceback.

npm gains rule 1 and rule 3 through the shared validator. Its other rules are unchanged.

### 4. Validate image references with the OCI distribution grammar

- Add one `image_reference` validator implementing the `distribution/reference` grammar:
  - optional domain (hostname or `[ipv6]`, optional port);
  - lowercase path components with `.`, `_`, `__` and `-` separators;
  - optional tag `[\w][\w.-]{0,127}`;
  - optional `@algorithm:hex` digest;
  - total name length at most 255.
- Use it in `argparse` type functions for `base_image` and `--image-tag`, so errors appear as usage errors.
- `--python` and `--rust` are validated as tag fragments: `[A-Za-z0-9][A-Za-z0-9._-]*`, with the full rendered tag (`<v>-slim`) at most 128 characters.
- The renderer functions also assert validity (raising `ValueError`). Direct library callers, such as the TUI, therefore get the same protection without going through `argparse`.

**Alternative considered:** asking the runtime to parse the reference (for example `docker image inspect`). It is runtime-specific, slow and network-dependent, and it runs after the rendering this change wants to protect. Rejected.

## Risks / Trade-offs

- [The splicer mishandles exec-form JSON with escaped quotes] → The splicer already passes JSON `COPY` through for npm, but not exec-form `RUN` with embedded `\"`. Add a splicing round-trip test with metacharacter member names. If it fails, extend the splicer's instruction tokenizer instead of reverting to shell text.
- [A legitimate image reference rejected by the grammar, for example an uppercase registry hostname] → Domain components are case-insensitive in the grammar, and only repository path components must be lowercase. Test with registry hostnames, ports, IPv6 literals and digests.
- [A project currently relying on a member name with a leading `-`] → This is vanishingly unlikely. The error names the member, so the user can rename it.

## Migration Plan

The generated Rust Dockerfile changes, so `--rust-cargo` images rebuild once. No configuration changes are needed.
