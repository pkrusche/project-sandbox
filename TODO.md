# TODO - outstanding items

## Verify API key injection on a real Apple `container` host

`--api-key-env` / `--api-key-env-file` now stage a 0600 `api-keys.env` file and
pass `--env-file` for the apple-container runtime, because unlike docker/podman
a bare `--env NAME` is not documented to inherit the value from the client's
environment. The argv/staging behavior is covered by
`tests/test_cli.py::ApiKeyInjectionTests`, but unit tests do not exercise the
real CLI by policy. Outstanding: on macOS, confirm `container run --env-file`
delivers the variable into the container, and check whether bare `--env NAME`
inheritance actually works there (if it does, the env-file path can be dropped
for symmetry with docker/podman).

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
