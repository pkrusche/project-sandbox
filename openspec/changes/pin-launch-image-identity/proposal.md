# Proposal

## Why

A session launches whatever image its tag points to when the container starts. Nothing ties that image to the inputs the session rendered.

- **Tag replacement (finding #6, launch half).**
  - The build lock (`cli.py:907`) covers the cache check and the build, but not the launch.
  - After it is released, a concurrent launch that uses the same tag with a different base image, CA certificate or flow can rebuild the tag before the first session's `run`.
  - The first session then starts a container whose entrypoint, privileged setup and trust store came from someone else's inputs.
  - The lock is keyed by the build context (`build_cache.py:45`), not by the image. Two projects, or a project and a session directory, that share an `--image-tag` hold different locks, so the lock doesn't even serialize their builds.
- **Stale images under `--no-build`.** `--no-build` only checks that the tag exists. An image built for a different base image, CA set or project-sandbox version is run as it is.
- **Incomplete fingerprint.** The build cache fingerprints a few named files (`build_cache._BUILD_INPUT_FILES`). It misses other generated build inputs, such as CA files and the generated ignore file. It also trusts a state file (`.build-state.json`) rather than the image itself.

`private-session-state` and `private-build-context` isolate the *inputs* per session. `mount-session-firewall` takes network policy out of the image. This change pins the *image*.

## What Changes

- **Images record what they were built from.** Every generated image is built with two labels:
  - `org.project-sandbox.inputs`: a fingerprint over *every* file in the session's generated build context, plus the settings that affect the image;
  - `org.project-sandbox.settings`: a small, secret-free JSON summary of those settings (base image, flow, installed agents, CA certificate hashes, build-time proxy endpoint, privilege model). It contains no network policy, because none is in the image.
- **The cache trusts the image, not a state file.** A build is skipped only when the image for the tag exists *and* its `inputs` label equals the session's fingerprint. `.build-state.json` is removed. Whole-project flows keep building every time, as today, and still get labels.
- **The build lock is keyed by the image.** It is taken per runtime and image repository, so every launch that could move a tag takes the same lock, whichever project it comes from.
- **Each session launches its own tag.**
  - While still holding the build lock, right after the build or cache hit, the session adds a tag unique to itself (`<repository>:session-<session-id>`).
  - The container is started from that tag, which no other launch moves.
  - The tag is removed at teardown. Session tags left by dead sessions are removed by the next launch.
- **`--no-build` verifies the image.** The session renders its inputs as usual, and compares its fingerprint with the image's `inputs` label.
  - On a mismatch, the launch fails before starting a container. It names the settings that differ, from the `settings` label, for example "base_image: image python:3.12-slim, session python:3.13-slim".
  - An image without labels, built by an older version, is refused the same way.
- **`--build-only` is unchanged.** After `mount-session-firewall`, no image content depends on the agent or on network options, so it has nothing agent-specific to build. It warns when given an option that doesn't affect the image, starting with `--pi-ollama`.

## Capabilities

### New Capabilities

- `image-identity`: how sandbox images record the inputs and settings they were built for, when an existing image may be reused, how a session pins the image it launches, and what `--no-build` guarantees.

### Modified Capabilities

(none: no existing spec covers the build cache, `--build-only` or `--no-build`)

## Impact

- **Code:**
  - `build_cache.py`: fingerprint over the whole session `build/` directory plus settings; label-based validity; the lock key; the state file removed.
  - `container_cli.py`: `--label` build args; label readback from `image inspect` per runtime; `image tag` and `image rm` per runtime.
  - `cli.py`: cache flow and session tag inside the build lock; the launch uses the session tag; session tags removed at teardown and reaped at launch; `--no-build` verification; the `--build-only` warning.
  - `dockerfile_checksum.py`: its baseline moves off `.build-state.json`'s location. It's otherwise unchanged.
- **Depends on:**
  - `private-session-state` and `private-build-context`, which make "every generated file" well-defined and private for all flows;
  - `mount-session-firewall`, which removes network policy from images;
  - `isolate-firewall-helpers`, whose privilege-model label check becomes one case of the `settings` comparison.
- **Runtimes:** Docker, Podman and Apple `container` all support `build --label`, `image tag` and `image delete`/`rm`. Label readback from Apple's `image inspect` output is checked in task 1.1.
- **Images:** existing tags rebuild once, because they have no labels. `--no-build` against an unlabeled image is refused with a rebuild hint.
- **TODO.md:** finding #6 is closed together with `private-session-state` and `private-build-context`.
- **Docs:** `docs/usage.md` ("Image Builds": labels, session tags, `--no-build` verification).
