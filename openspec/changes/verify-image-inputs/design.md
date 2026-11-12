# Design

## Context

- Cache flow (`cli.py`, under `build_cache.build_lock(context)` and
  `image_lock(runtime)`): fingerprint named generated files plus
  `{image_tag, runtime, base_image, host_identity}`; reuse when
  `.build-state.json` matches and the tag's identity equals the recorded one;
  otherwise build and write the state file. Whole-project contexts always
  build.
- `--no-build` checks `image_exists(tag)`, then pins it.
- Docker, Podman and Apple `container` support `build --label`; label readback
  comes from `image inspect`, whose JSON shape differs (Apple nests it under
  the image's config).

## Goals / Non-Goals

**Goals:** a reused or `--no-build` image is one built from the session's
inputs; mismatches are explained.

**Non-Goals:** reproducible builds or digest-pinned base images; fingerprinting
project source for whole-project flows; the sidecar image (content-tagged
already); session pinning (exists).

## Decisions

### 1. Labels are the source of truth

- `inputs`: SHA-256 over sorted `(relative path, sha256(content))` of every
  regular file in the generated build context directory the build reads
  (symlinks refused), plus canonical JSON of `settings` and `host_identity`.
  The image tag is not part of it, so the same inputs under another tag match.
- `settings`: canonical JSON of `base_image`, `flow`, `install_agents`,
  `ca_certificates` (SHA-256 each), `build_proxy` (endpoint, no credentials),
  and `firewall_delivery` (`baked` today, `mounted` after
  `mount-session-firewall`). A renderer assertion rejects any value equal to a
  known secret.
- Reuse iff the tag exists and its `inputs` label equals the fingerprint. The
  existing identity check and session alias still follow.

*Alternative:* keep the state file and add labels for `--no-build` only. Two
sources of truth that can disagree. Rejected.

### 2. `--no-build` verification

Render inputs, compute `inputs`, read both labels. Match: pin and launch.
Mismatch or missing: print a key-by-key diff of `settings` (for example
`base_image: image python:3.12-slim, session python:3.13-slim`); an empty diff
means a project-sandbox or template change; refuse with "rebuild with
`--build-only` or drop `--no-build`".

### 3. `--build-only`

Unchanged gating. It warns when given options that don't affect the image.
Until `mount-session-firewall` lands, firewall options still do, so the list
then is only options with no build effect at all.

## Risks / Trade-offs

- [Apple label JSON shape] → Fixture tests per runtime from captured
  `image inspect` output (task 1.1).
- [One-time rebuild after upgrade] → Expected.
- [A new image-affecting option missing from `settings`] → `inputs` still
  covers it through rendered files; a test asserts every such option appears
  in `settings`, which only explains mismatches.
