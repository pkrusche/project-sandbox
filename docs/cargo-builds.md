# Generated Rust builds

`--rust-cargo` generates a Rust base Dockerfile with dependency fetching and a
best-effort project build. When both `Cargo.toml` and `Cargo.lock` exist, workspace
member manifests are copied before source files to preserve the dependency cache.

Member paths are treated as literal filenames in generated shell commands and
Dockerfile `COPY` instructions. Spaces, quotes, dollar signs, semicolons, leading
hyphens, and literal wildcard characters are supported. Workspace member glob
patterns are expanded on the host; discovered members are sorted and deduplicated.
Dockerfile copies use JSON arrays plus escaping for Docker's variable processing
and source wildcard matching, as described in the
[Dockerfile reference](https://docs.docker.com/reference/dockerfile/#copy).

Absolute paths, parent traversal, control characters, Unicode line separators,
and backslashes are rejected. Member directories and their `Cargo.toml` files
must resolve inside the project; symlinks that escape it fail validation.
Validation also runs during `--dry-run`, which writes no generated files.
These checks describe the inspected filesystem; they do not freeze a build
context against concurrent edits by other processes.

The positional base image must be a literal image-reference token. `--rust` and
`--python` values must fit an image tag after the generated `-slim` suffix is
added. Whitespace, new instructions, variable expressions, and shell syntax are
rejected before interpolation. The runtime still checks image-reference
semantics and availability. User-supplied Dockerfiles and project build scripts
remain trusted build inputs; path quoting does not sandbox their execution.
