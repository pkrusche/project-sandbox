# Proposal

## Why

A session launches whatever image its tag points to when the container starts. Nothing ties that image to the inputs the session rendered.

- **Tag replacement (finding #6, launch half).**
  - The build lock (`cli.py:907`) covers the cache check and the build, but not the launch.
  - After it is released, a concurrent launch for the same project, with a different allowlist, proxy or agent setting, can rebuild the shared tag before the first session's `run`.
  - The first session then starts a container whose firewall and entrypoint came from someone else's inputs. A session that asked for a restrictive policy can get a more permissive image.
  - `private-session-state` already isolates the *inputs* per session. This change pins the *image*.
- **Stale images under `--no-build` (TODO.md item 1).** `--no-build` only checks that the tag exists.
  - `--build-only` rejects `--agent`, and `--pi-ollama` takes effect only with `--agent pi`. So `--build-only --pi-ollama` silently builds an image without the Ollama firewall rule.
  - A later `--agent pi --pi-ollama --no-build` session runs that image, and Ollama fails with "No route to host".
  - The same mismatch can happen for any setting rendered into the image: extra domains, egress mode, proxies, CA certificates.
- **Incomplete fingerprint.** The build cache fingerprints four named files (`build_cache._BUILD_INPUT_FILES`). It misses other generated build inputs, such as CA files and the generated ignore file. It also trusts a state file (`.build-state.json`) rather than the image itself.

## What Changes

- **Images record the settings they were built for.** Every generated image is built with two labels:
  - `org.project-sandbox.inputs`: a fingerprint over *every* file in the session's generated build context, plus the settings that affect the image;
  - `org.project-sandbox.settings`: a small, secret-free JSON summary of the policy-relevant settings (egress mode, extra domains, Ollama, agent proxy endpoint and model list, Internet proxy endpoint, CA certificate hashes, installed agents, base image).
- **The cache trusts the image, not a state file.** A build is skipped only when the image for the tag exists *and* its `inputs` label equals the session's fingerprint. `.build-state.json` is removed. Whole-project flows keep building every time, as today, and still get labels.
- **Each session launches its own tag.**
  - While still holding the build lock, right after the build or cache hit, the session adds a tag unique to itself (`<repository>:session-<session-id>`).
  - The container is started from that tag, which no other launch moves.
  - The tag is removed at teardown. Session tags left by dead sessions are removed by the next launch.
- **`--no-build` verifies the image.** The session renders its inputs as usual, and compares its fingerprint with the image's `inputs` label.
  - On a mismatch, the launch fails before starting a container. It names the settings that differ, from the `settings` label, for example "image built without `--pi-ollama`".
  - An image without labels, built by an older version, is refused the same way.
- **`--build-only` accepts `--agent`**, together with agent-dependent options such as `--pi-ollama` and `--agent-proxy`, so it builds exactly the image a later session of that agent needs. A prompt and `--branch` stay rejected. Without `--agent`, `--build-only` warns about any agent-dependent option it ignores, starting with `--pi-ollama`.

## Capabilities

### New Capabilities

- `image-identity`: how sandbox images record the inputs and settings they were built for, when an existing image may be reused, how a session pins the image it launches, and what `--build-only` and `--no-build` guarantee.

### Modified Capabilities

(none: no existing spec covers the build cache, `--build-only` or `--no-build`)

## Impact

- **Code:**
  - `build_cache.py`: fingerprint over the whole session `build/` directory plus settings; label-based validity; the state file removed.
  - `container_cli.py`: `--label` build args; label readback from `image inspect` per runtime; `image tag` and `image rm` per runtime.
  - `cli.py`:
    - cache flow and session tag inside the build lock;
    - the launch uses the session tag;
    - session tags removed at teardown and reaped at launch;
    - `--no-build` verification;
    - `--build-only` with `--agent`.
  - `dockerfile_checksum.py`: its baseline moves off `.build-state.json`'s location. It's otherwise unchanged.
- **Depends on:**
  - `private-session-state`, whose per-session `build/` directory makes "every generated file" well-defined and private;
  - `isolate-firewall-helpers`, whose privilege-model label check becomes one case of the `settings` comparison.
- **Runtimes:** Docker, Podman and Apple `container` all support `build --label`, `image tag` and `image delete`/`rm`. Label readback from Apple's `image inspect` output is checked in task 1.1.
- **Images:** existing tags rebuild once, because they have no labels. `--no-build` against an unlabeled image is refused with a rebuild hint.
- **TODO.md:** item 1 is resolved, and finding #6 is closed together with `private-session-state`.
- **Docs:** `docs/usage.md` ("Image Builds": labels, session tags, `--build-only --agent`, `--no-build` verification).
