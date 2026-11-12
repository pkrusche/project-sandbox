# Proposal

## Why

This line pins the image a session launches: each session holds a private
`localhost/project-sandbox-session:<random>` alias with a verified content
identity, taken under a per-runtime image-store lock (`47c8f06`, `150b013`).
What it doesn't do is tie that image to the inputs the session rendered:

- **`--no-build` runs whatever the tag holds.** It only checks that the tag
  exists. An image built for a different base image, CA set, flow or
  project-sandbox version runs as it is. The security e2e suite needed its own
  input check to share images safely for exactly this reason.
- **The cache trusts a state file.** `.build-state.json` in the project's
  generated directory records a fingerprint over a few named files
  (`build_cache._BUILD_INPUT_FILES` plus CA files). Other generated inputs are
  not covered, and a context directory change loses the record.

The `sec-fixing-claude` branch's `pin-launch-image-identity` draft solved both
with image labels; its session-tag part already exists here in another form.
This change ports the label part.

## What Changes

- Every generated image is built with two labels:
  - `org.project-sandbox.inputs`: a fingerprint over every file of the
    generated build context the image is built from, plus the image-affecting
    settings;
  - `org.project-sandbox.settings`: secret-free JSON of those settings (base
    image, flow, installed agents, CA certificate hashes, build-time proxy
    endpoint without credentials, firewall delivery).
- The cache reuses an image only when the tag's `inputs` label equals the
  session's fingerprint; `.build-state.json` is no longer consulted.
- `--no-build` renders inputs as usual and refuses an image whose `inputs`
  label differs or is missing, printing the `settings` keys that differ.
- `--build-only` warns about options that don't affect the image.
- Unchanged: session aliases, the image-store lock, whole-project flows that
  always build.

## Capabilities

### New Capabilities
- `image-identity`: how images record the inputs and settings they were built
  for, when an image may be reused, and what `--no-build` guarantees.

### Modified Capabilities

None.

## Impact

- Code: `build_cache.py` (fingerprint over the whole generated context,
  label-based validity), `container_cli.py` (`--label` build args, label
  readback per runtime), `cli.py` (cache flow, `--no-build` verification,
  `--build-only` warnings), `dockerfile_checksum.py` (baseline location).
- Images rebuild once (no labels yet). `--no-build` against an unlabeled image
  is refused with a rebuild hint.
- Interplay: after `mount-session-firewall`, network options leave the inputs,
  so `settings` carries none; the security e2e suite's own input guard can then
  rely on `--no-build` verification.
- Docs: `docs/usage.md` ("Image builds"), `docs/runtime.md` ("Concurrent sessions").
