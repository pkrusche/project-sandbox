# TODO - outstanding items

## Firewall: verify multi-resolver rules on a real iptables host

Code is complete and the render path is covered by
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`.
The unit tests are render-only by policy and do not exercise live iptables.
Outstanding: run the rendered script on a host with iptables and multiple
`nameserver` entries in `resolv.conf`, then confirm allowlisted-domain
pre-resolution works across the resolver setup and post-firewall DNS egress does
not leak before treating this as shipped.

## Ensure we don't mount the Dockerfile used by --dockerfile

This way the agent cannot modify its own running environment.

## Isolate concurrent subagents in separate clones, merge back on teardown

Every `--branch` jj agent shares one repo's `.jj/repo` store and — since we now
also mount the git backend — its `.git`, both bind-mounted read-write into each
container. That fits jj's concurrent-workspace model on a shared-kernel runtime,
but concurrent writes from *inside* multiple containers to a single shared store
are not obviously safe across separate VMs (Apple `container` + VirtioFS), where
lock-file and rename atomicity may not hold.

Plan: give each subagent its own clone, then merge/rebase the agent's bookmark 
back into the parent repo during teardown. This removes the shared-store race 
entirely and keeps each agent's blast radius isolated.

Note the git-worktree (`--branch` non-jj) path — which shares `.git` the same way —
should use the same approach.

Interim mitigation already in place: a host-side exclusive lock serializes
`jj_workspace.teardown()` (`_teardown_lock`), so concurrent teardowns can't
interleave their store mutations. It does not address concurrent in-container
writes; this item supersedes it.

## Dockerfile tamper-detection: only record the baseline after an actual build

Review of the `hide-dockerfiles` change (`dockerfile_checksum.py` + its CLI
wiring) found the checksum baseline is recorded too eagerly, which weakens the
very tamper-evidence the feature is meant to provide.

### Problem

`dockerfile_checksum.record(context_dir, tracked_dockerfiles)` is called at
`cli.py:395`, *outside* the `if not args.no_build:` block (it sits after it at
the same indentation). So the trusted baseline is overwritten with the current
Dockerfile's checksum on **every** run that reaches the agent step, regardless of
whether a build from that Dockerfile actually happened:

- `--no-build` runs (reachable for the only tracked case, `--dockerfile`): no
  build occurs, yet the current — possibly agent-modified — Dockerfile is
  recorded as the new trusted baseline.
- Cache-hit / `--no-build` generally: the image is reused or not built, but the
  baseline is still advanced to "current".

Effect on the threat model: an agent edits the `--dockerfile` during session N.
On run N+1 the `[W]` warning fires once (`changed_warnings`, `cli.py:340`), but
`record()` immediately overwrites the baseline with the tampered checksum. Run
N+2 is then silent even though the tampered Dockerfile was never reviewed and
(under `--no-build`) never built. The warning is effectively one-shot and
self-clearing, so persistent tamper-evidence is lost.

This also contradicts the documentation, which states recording happens *after a
build*:
- `docs/runtime.md`: "After each build, project-sandbox records a SHA256 of the
  Dockerfile".
- `docs/security.md`: warns "when the Dockerfile changed since it was last
  built".
- The code comment at `cli.py:392-394` itself says "after building from them" —
  but the call runs regardless.

### Fix plan

1. **Gate the record on an actual build.** Move/guard the
   `dockerfile_checksum.record(...)` call so it only runs when a real build from
   the tracked Dockerfile happened — i.e. inside the build branch, on the
   non-cache-hit path, after `build_cache.write_state(...)` succeeds
   (`cli.py:385-390`). At minimum guard with `if not args.no_build:`; preferably
   also skip on `cache_hit` so the baseline always means "what we last actually
   built". (For the only tracked case today — `--dockerfile`, which lives outside
   `.project-sandbox` — `context_is_sandbox` is False so `cache_hit` is always
   False; the urgent, reachable gap is `--no-build`. Handling cache-hit too keeps
   the invariant robust if tracking is later extended.)
2. **Keep the warning where it is** (`cli.py:340`, before the build): the user
   should still see the change before choosing to rebuild. With the record moved
   into the real-build path, the warning correctly persists across cache-hit /
   `--no-build` runs until a genuine rebuild re-establishes the baseline.
3. **Align the comment** at `cli.py:392-394` with the actual (post-fix) behavior.
4. **Add a regression test** in `tests/test_cli.py`: a `--dockerfile ... --no-build`
   run must *not* update `.dockerfile-checksums.json`, so a subsequent run still
   warns. Assert the state file is unchanged (or that the warning still fires) —
   mirrors the existing
   `test_dry_run_warns_when_project_dockerfile_changed` setup. Confirm the
   existing `tests/test_dockerfile_checksum.py` cases still hold.

### Verification

```bash
uv run python -m compileall src tests
uv run pytest -q
```

## Minor: stale line reference in the Docker Sandbox ROADMAP section

The ROADMAP "Docker Sandbox (`sbx`)" section states `SUPPORTED_AGENTS` is at
`cli.py:41`; it is actually `cli.py:43`
(`SUPPORTED_AGENTS = ("claude", "codex", "opencode", "bash")`). Low priority —
fix the reference (or drop the exact line number, which drifts) when that section
is next touched.
