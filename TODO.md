# TODO - outstanding items

## Firewall: verify multi-resolver rules on a real iptables host

Code is complete and the render path is covered by
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`.
The unit tests are render-only by policy and do not exercise live iptables.
Outstanding: run the rendered script on a host with iptables and multiple
`nameserver` entries in `resolv.conf`, then confirm allowlisted-domain
pre-resolution works across the resolver setup and post-firewall DNS egress does
not leak before treating this as shipped.

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
`jj_workspace.finalize()` (`_teardown_lock`), so concurrent teardowns can't
interleave their store mutations. It does not address concurrent in-container
writes; this item supersedes it.

## Docker/Podman on Linux: fixed UID 1000 breaks when the host user isn't UID 1000

`templates/Dockerfile.j2` hardcodes the in-container `agent` user to UID/GID
1000 unconditionally, and `container_cli.py`'s `docker`/`podman` run path does
no host-UID matching. Apple `container` sidesteps this via VirtioFS UID
remapping, and `--runtime chroot` sidesteps it via `unshare --map-root-user`,
but plain Docker/Podman on Linux has none: if the host user's UID isn't 1000
(the common case for the first account on many single-user desktop distros,
but not guaranteed), the container can't write to the bind-mounted
`/workspace` (or the git worktree / `.git` metadata mount in `--branch`
mode), and files it does create come back owned by a UID the host user can't
always clean up either.

Discovered via the CI e2e workflow's git/jj tests under `--runtime docker`
(`.github/workflows/e2e.yml`), where the runner's UID differs from 1000. The
git test's version of this is worked around locally (`umask 000` + `chmod` on
the throwaway repo, plus a `sudo rm -rf` cleanup fallback, in
`scripts/e2e-git-workflow.sh`), since git fully respects `umask` for its own
files. jj does not: it hardcodes `0600` on its working-copy/store state files
(`checkout`, `tree_state`, `op_store/*`, …) regardless of `umask`, so the
host-side `jj_workspace.finalize()` step — which runs natively as the host's
own UID right after the container exits, in the *same* process, so there is
no window for an external script to intervene — fails with `Failed to read
checkout state: Permission denied` trying to read those files back. A
container-side `chmod -R 777` on both `.jj` directories (added to
`scripts/e2e-jj-workflow.sh`'s agent prompt) did not resolve it in CI, so the
jj e2e workflow step is commented out in `.github/workflows/e2e.yml` until
this is fixed. The underlying product gap (real users on Linux with
Docker/Podman hitting the same wall on `--branch` jj sessions, not just CI)
is still unfixed either way.

Fix suggestion: Pass the host UID/GID as a Docker build arg and create `agent` with it at
image-build time. Matches today's fixed-user model more closely, at the
cost of an extra build parameter and losing image-cache reuse across
different host users/machines.
