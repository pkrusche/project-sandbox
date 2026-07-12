# TODO - outstanding items

## Release script

Create a scripts/make-release.sh script with the following functionaliity:

* run checks (ruff, pytest)
* bump version (confirm version with user / keep version)
* Create a GH release and tag using the gh cli
* Push to test.pypi.org
* Push to pypi.org

Each of these steps should gate the next, keep a local folder (not versioned / gitignored) with the release status, check before each step that the working copy is clean / has no changes (note when we use jj we should use a temporary revision for that).

The final pushes to testpypi / pypi need to be confirmed by the user.

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

## Wire oauth_refresh.py / token_expiry.py for Pi

Pi (pi.dev) agent support has shipped (see the `add-pi-agent-support`
OpenSpec change): image install, credential mounting
(`/project-sandbox-secrets/pi`, flat `auth.json`), headless/interactive
dispatch, combined `--model <model>:<effort>` flag, and provider-allowlist
warning. Deliberately deferred, since both modules are defensive by
construction (unknown agent ⇒ silent no-op / `None`, never raises) and it's
safer to under-wire than guess wrong field names:

- `oauth_refresh.py`: does Pi have a CLI subcommand like `codex login status`
  to delegate a host token refresh to?
- `token_expiry.py`: what is the JSON shape of an OAuth entry in
  `~/.pi/agent/auth.json` — flat like Codex's, or a provider-keyed map like
  OpenCode's, given Pi is also multi-provider?

Confirm both against Pi's actual source, then wire them the same way Codex
(flat) or OpenCode (provider-keyed) is handled, whichever shape matches.
