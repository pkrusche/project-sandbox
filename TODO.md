# TODO - outstanding items

## Dummy chroot runtime for filesystem-layout verification

A `--runtime chroot` "dummy" runtime that, on Linux, reproduces only the
container's *filesystem layout* inside a rootless chroot jail so the mount/dir
arrangement can be inspected without docker/podman/apple-container. It is for
verification, not isolation: no image build, no firewall, no network namespace,
and the agent toolchain (node, claude, codex, …) is NOT installed in the jail.

The goal (and verification strategy) of this task is to ensure the end to end
tests in the scripts/ directory can all run in CI without access to a container
runtime, and with minimal overhead.

### Behavior

- Linux-only. `select_runtime("chroot")` returns it only when requested
  explicitly; `auto` never selects it. Error clearly on non-Linux.
- Privileges: rootless via `unshare --map-root-user --mount` — no sudo. Bind
  mounts and `chroot` happen inside a private user+mount namespace.
- Image build is a no-op: `build_image()` and `ensure_system_started()`
  short-circuit (return 0) for this runtime; no Dockerfile is rendered/built.
- The jail mirrors the container mount set onto a temporary jail root:
  - host system dirs (`/usr`, `/bin`, `/lib*`, `/etc`) bind-mounted read-only so
    a shell works inside the jail;
  - `<workspace>` -> `/workspace`;
  - the `agent` home skeleton (`/home/agent/.claude`, `.codex`, `.config`);
  - `/project-sandbox-config/{claude,codex}` and
    `/project-sandbox-secrets/{claude,codex,opencode}` from the staged dirs;
  - the prompt mount and the read-only masks over `/workspace/.project-sandbox`
    and `/workspace/.devcontainer`, matching the real run path.
- Entry: drops into `bash` in the jail (and/or prints the resulting tree). It
  does not exec the coding agent — there is no agent CLI inside the jail.
- `--dry-run` stays faithful: print the `unshare`/`chroot` argv and the planned
  bind mounts; create nothing and mount nothing.

### Implementation sketch

- container_cli.py: add `CHROOT = Runtime("chroot", "unshare")`, register it in
  `RUNTIME_CHOICES`/`_RUNTIMES`, and gate it in `select_runtime` (explicit-only,
  Linux-only). Add `build_chroot_argv(...)` rather than overloading
  `build_run_argv()`, since the argv shape (no `run --mount`, no image) differs.
- Refactor the mount set into a shared structured form (e.g.
  `MountSpec(source, target, readonly)`) consumed by BOTH the existing container
  argv path and the new chroot path, so the two layouts cannot drift. This is the
  main architectural lift — today the mounts are assembled inline in
  `build_run_argv()` and `_build_session_command()` in cli.py.
- New template `templates/chroot-run.sh.j2` rendered to
  `.project-sandbox/chroot-run.sh`: makes the jail root, creates target dirs,
  bind-mounts each `MountSpec` (`-o ro` for readonly), then `chroot` + exec shell.
  Follow the existing template/`render_*` pattern in dockerfile.py.
- cli.py: branch the `chroot` runtime past `ensure_system_started`, the
  Dockerfile render/build, the cache check, and the firewall — none apply.

### Tests (render-/argv-only, never mount for real)

- `auto` never selects `chroot`; explicit `--runtime chroot` does; non-Linux errors.
- `build_chroot_argv` maps the same sources/targets/readonly flags as the
  container mount set (assert via the shared `MountSpec` list).
- `chroot-run.sh` renders the expected dirs/binds and uses
  `unshare --map-root-user --mount`.
- `--runtime chroot --dry-run` writes/mounts nothing.

### Notes / risks

- Not a security boundary. chroot + a single-UID user-ns map is for inspection
  only; document this prominently in docs/runtime.md so it is never mistaken for
  the real sandbox isolation.
- Rootless bind mounts surface non-mapped host UIDs as `nobody`; acceptable for
  layout inspection.
- Keep the chroot mount list sourced from the shared `MountSpec` set so it tracks
  future changes to the real run mounts automatically.

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

Candidate fixes:
- Run the container with `--user "$(id -u):$(id -g)"` instead of the fixed
  `agent` UID. Simplest, but `$HOME=/home/agent` and anything else keyed to a
  known UID (credential-mount ownership, the `/etc/passwd` entry) needs
  rechecking for an arbitrary host UID.
- Pass the host UID/GID as a Docker build arg and create `agent` with it at
  image-build time. Matches today's fixed-user model more closely, at the
  cost of an extra build parameter and losing image-cache reuse across
  different host users/machines.
