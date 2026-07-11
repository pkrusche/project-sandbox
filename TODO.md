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

## Add pi.dev ("pi") coding agent support

Add a fourth supported agent: Pi, from pi.dev (`earendil-works/pi`, npm
package `@earendil-works/pi-coding-agent`, binary `pi`, current published
version `0.80.6`). There is no shared agent registry in this codebase —
"codex" and "opencode" are hardcoded by name across `cli.py`,
`container_cli.py`, `config_agents.py`, `dockerfile.py`/`Dockerfile.j2`,
`devcontainer.py`, `entrypoint.sh.j2`/`_provision.sh.j2`, `oauth_refresh.py`,
`token_expiry.py`, tests, and docs — adding Pi means touching each the same
way.

Pi's shape: config/session dir is `~/.pi/agent/` (`PI_CODING_AGENT_DIR`
override); credentials are a single `~/.pi/agent/auth.json` file, mode 0600
(API key string or OAuth token object), no macOS Keychain — closer to Codex's
`auth.json` than Claude's Keychain dance. Auth via env var, OAuth `/login`,
or 20+ BYOK providers. Crucially, **Pi has no built-in permission/confirmation
system** ("run in a container, or build your own confirmation flow with
extensions"), so unlike Claude/Codex there's no bypass-permissions config to
bake into a file — telemetry/update-check suppression is via env vars
(`PI_SKIP_VERSION_CHECK=1`; confirm `PI_TELEMETRY`'s disable value before
relying on it) instead. `--approve`/`-a` trusts project-local `.pi/` config
for one run and should always be passed (headless can't answer an interactive
trust prompt). Headless invocation is `pi -p "prompt" --approve`, emitting
plain text (fits `session.py`'s existing log-teeing, no JSON transcript
format needed). Model/effort is a single combined flag:
`--model sonnet:high`, not two separate flags.

Overall pattern: follow **OpenCode** for "no baked config file to
render/mount, BYOK ⇒ warn instead of hardcoding a firewall domain, no VS Code
extension"; follow **Codex** for "credentials are one flat file" (reuse
`_sync_generic_credentials(..., include_files=("auth.json",))`). Concretely:
`/project-sandbox-secrets/pi` mount but no `/project-sandbox-config/pi`; new
`pi`/`pi-headless` case arms in `entrypoint.sh.j2`; new npm-install block in
`Dockerfile.j2` behind an `install_pi` flag; `"pi"` added to
`SUPPORTED_AGENTS`, `_CONFIGURED_AGENTS`, `_agent_host_paths`,
`credential_dirs` comprehensions, and `build_mount_specs`/`build_run_argv`
call sites; generalize `_warn_opencode_provider_allowlist` to cover both
OpenCode and Pi; extend `devcontainer.py`'s credentials-only mount pattern
(mirroring OpenCode) and `_credential_dirs()`; no transcript.py renderer for
now (Pi's headless output isn't structured JSON yet — matches the OpenCode
precedent of no markdown transcript); mirror codex/opencode test coverage in
`test_cli.py`, `test_container_cli.py`, `test_renderers.py`,
`test_devcontainer.py`; update `README.md`, `docs/usage.md`,
`docs/runtime.md`, `docs/security.md`.

Before wiring `oauth_refresh.py` (does Pi have a CLI subcommand like `codex
login status` to delegate a refresh to?) and `token_expiry.py` (exact JSON
shape of an OAuth entry in `auth.json` — flat like Codex's or a
provider-keyed map like OpenCode's, given Pi is also multi-provider?),
confirm against Pi's actual source. Both modules are defensive by
construction (unknown agent ⇒ silent no-op / `None`, never raises), so it's
always safe to under-wire these rather than guess wrong field names — can
ship without them and fill in once confirmed, same as OpenCode currently has
no delegated refresh.
