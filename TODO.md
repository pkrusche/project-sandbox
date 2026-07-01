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
