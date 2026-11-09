# Design

## Context

- **Cache flow** (`cli.py:896–983`):
  1. Under `build_cache.build_lock(context_dir)`, a lock keyed by the context directory, compute a fingerprint over a few named generated files, plus `{image_tag, base_image, host_identity}`.
  2. Compare it with `.build-state.json`, and check `image_exists(tag)`.
  3. Build if needed, then write the state file.
  4. Release the lock.
  5. Later, `run` references `args.image_tag`.

  Whole-project contexts (`--python-uv`, `--rust-cargo`, `--node-npm`, `--dockerfile`) never skip the build.
- **`--no-build`** only checks `image_exists(tag)` (`cli.py:974`).
- **`--build-only`** rejects `--agent` (`cli.py:546`). Images install every available agent (`install_agents=available_agents`, `cli.py:749`), and after `mount-session-firewall` they hold no network policy, so no image content depends on the selected agent.
- **Apple `container` 1.7.1** has `build --label`, `image tag`, `image inspect` and `image delete`. Docker and Podman have the equivalents.
- After `private-session-state` and `private-build-context`, every generated build input for a session lives in its private `staging/<id>/build/`, and builds read them only from there.

## Goals / Non-Goals

**Goals:**
- What a session runs is exactly what it built or verified.
- `--no-build` can't silently run an image built from different inputs.

**Non-Goals:**
- Reproducible builds, or pinning base images by digest. The base image *reference* is part of the settings, and upstream tag movement is unchanged.
- Fingerprinting the project source for whole-project builds. Those keep rebuilding, relying on the layer cache.
- The egress sidecar image, which is already tagged by its own content hash (`sidecar_image.image_tag`).

## Decisions

### 1. Labels on the image are the source of truth

- **`inputs` fingerprint:** a SHA-256 over the sorted `(relative path, sha256(content))` of every regular file under the session `build/` directory (symlinks refused, as `private-session-state` guarantees), plus a canonical JSON of `settings` and `host_identity`.
- **`settings`:** a canonical JSON object containing `base_image`, `flow` (default, python-uv, rust-cargo, node-npm or dockerfile), `install_agents`, `ca_certificates` (SHA-256 of each), `build_proxy` (the build-time proxy endpoint without credentials) and `privilege_model`. Network policy isn't in it: the firewall is a runtime input (`mount-session-firewall`). The renderer asserts that no `settings` value equals a known secret.
- **Build:** passes `--label org.project-sandbox.inputs=<fp>` and `--label org.project-sandbox.settings=<json>` on all three runtimes.
- **Cache validity:** `image inspect <tag>` returns the labels, and the build is skipped iff `inputs == fp`. `.build-state.json` and `write_state` are removed. A tag replaced by anything else simply misses the cache.

**Alternative considered:** keep the state file and add labels only for `--no-build`. That keeps two sources of truth that can disagree. Rejected.

### 2. Session tags taken under a lock keyed by the image

```
with build_lock(runtime, repository):
    fp = fingerprint(session build/)
    if not (labels(tag).inputs == fp): build -t tag --label …
    image tag <tag> <repo>:session-<session_id>          # atomic w.r.t. other builds
run … <repo>:session-<session_id>
finally: image rm <repo>:session-<session_id>          # untag only
```

- The repository part comes from `--image-tag` or the default tag (`project-sandbox-<name>-<hash>`).
- **Lock key:** `host_locks.path_lock("image", "<runtime>/<repository>")`. Keying by project or context would let two launches that share an `--image-tag` from different projects, or from a project and a session directory, build and tag at the same time.
- The tag is created while the lock is still held, so no other build can run between "verified" and "tagged". From then on, only this session uses the tag.
- **Removal untags:**
  - Docker: `rmi <session tag>`, which keeps an image that has other tags.
  - Podman: `untag`.
  - Apple: `image delete <session tag>`. Whether that removes only the reference when other tags exist is checked in task 1.1. If it deletes the content, Apple keeps session tags and prunes them by age instead.
- **Reaping:** at launch, list the session tags for the project's repository, and remove those whose session lease (`private-session-state`) can be taken, meaning their owner is dead.

**Alternative considered:** launch by image ID or digest. Docker and Podman accept IDs, but `container run` support for ID references isn't documented. Per-session tags work the same on all three runtimes, and also show up readably in `image ls`.

### 3. `--no-build` verification and the settings diff

`--no-build` runs the full render into the session `build/` directory, computes `fp`, and reads both labels from the image:
- If `inputs` matches: tag the session and launch.
- If it differs or is missing: compute a key-by-key diff between the image's `settings` and the session's, and print each difference, for example "base_image: image python:3.12-slim, session python:3.13-slim". Then refuse. An empty `settings` diff with a different `inputs` means a changed project-sandbox version or template; the message says to rebuild.

### 4. `--build-only`

`--build-only` keeps rejecting `--agent`, a prompt, `--branch` and `--json-summary`. It builds and labels the image, and adds no session tag. When it is given an option that doesn't affect the image (`--pi-ollama`, `--extra-domain`, `--agent-proxy`, `--egress`), it warns that the option has no effect on the build.

## Risks / Trade-offs

- [Apple `image inspect` output may not expose labels in the same JSON shape] → Task 1.1 captures real output for all three runtimes, and the parser gets fixture tests per runtime.
- [Apple `image delete` semantics] → See Decision 2's fallback.
- [More tags in `image ls`] → Session tags exist only while a session runs, or until the next launch reaps them.
- [Version upgrades rebuild every project once, because there are no labels yet] → Expected and one-time.
- [Settings summary drift, meaning a new image-affecting option not added to `settings`] → `inputs` still covers it, because the option changes rendered files. `settings` exists only to explain mismatches. A test asserts that every option affecting rendered build files appears in `settings`.

## Migration Plan

- The first launch after upgrade rebuilds, because there are no labels.
- `--no-build` users rebuild once with `--build-only`.
- `.build-state.json` files left by older versions are ignored, and removed by `private-session-state`'s migration.
