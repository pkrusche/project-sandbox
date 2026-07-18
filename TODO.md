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

## Disable telemetry for Openspec

The variable to set in the container for this is OPENSPEC_TELEMETRY=0.

## --dockerfile: reorder template splicing so cache-stable installs run before user content

Today, `dockerfile.render()` / `templates/Dockerfile.j2` splice a user-supplied
`--dockerfile`'s entire content in as a prefix, and only *after* it append the
template's own dependency installs (`apt-get`, Node.js, jj, and the per-agent
`npm install -g` calls) plus the `agent` user/firewall setup. Since Docker's
build cache invalidates every layer after the first changed one, any
source-dependent step in the user's own Dockerfile (typically a `COPY src/` for
cache-warming a language toolchain) sits *before* those expensive, otherwise
base-image-only installs — so an ordinary source edit forces Node.js/jj/all the
agent CLIs to reinstall on every rebuild, even though none of them depend on
the user's source at all.

This was found while trying to make `/workspace/Dockerfile`'s own uv
cache-warming step (already split into a deps-only layer + a project-install
layer, see the two-layer `COPY pyproject.toml uv.lock` / `COPY src/` blocks)
rebuild-friendly: splitting the layers inside that one file only helps the
small `uv sync` cost — it does nothing for the much larger Node.js/jj/npm
reinstall cost, because those live in the template and always come after the
whole user fragment regardless of how the fragment itself is internally
ordered.

Considered and rejected as insufficient:
- Multi-stage build copying only `/opt/uv-cache` forward: uv itself writes
  source/commit-keyed editable-build entries into that cache dir, so the
  copied artifact still changes on most source edits and the invalidation
  cascade still happens.
- Dropping the project-install step and pre-fetching just the `[build-system]
  requires` (e.g. `hatchling`) directly: works, but only for this repo's own
  Dockerfile — doesn't fix the general `--dockerfile` splicing order for other
  projects' custom Dockerfiles.

### Plan

Reorder the merged Dockerfile to:

1. `FROM` — pulled from the user-supplied Dockerfile (or `base_image` when
   none was given).
2. The template's own dependency installs: `apt-get`, Node.js, jj, and the
   per-agent `npm install -g` calls. These only ever depend on the base image,
   never on user content, so they belong immediately after `FROM`.
3. The sanitized *rest* of the user's Dockerfile (everything after its own
   `FROM`) — `ARG`/`COPY`/`RUN` and whatever else the project needs, including
   any source-dependent cache-warming.
4. The `agent` user & firewall setup: `useradd`/`groupadd`, the config/secret
   directories, the firewall/entrypoint `COPY`s, `chmod`/sudoers, `USER agent`,
   `WORKDIR`, `ENTRYPOINT`/`CMD`. This already runs after user content today,
   so its position relative to step 3 is unchanged — only its position
   relative to step 2 moves (today it's interleaved with dependency installs;
   it becomes its own trailing block).

`USER root` continues to be inserted right after `FROM`/`ARG` (before step 2),
since apt-get and the rest all need root; the switch to `USER agent` still
only happens at the very end of step 4.

Implementation:

- `src/project_sandbox/dockerfile.py`: `render()` needs to split the
  *sanitized* source Dockerfile text (post `_remove_restricted_user_setup`)
  into a "from part" and a "rest part". Split at the last top-level `FROM`
  block, reusing `_dockerfile_blocks`/the same rule `_extract_last_from`
  already uses to find the effective base image — this keeps any earlier
  named stages in a multi-stage user Dockerfile intact (they land in the "from
  part" alongside the final stage's `FROM`), and only the final stage's tail
  becomes the "rest part" spliced in at step 3.
- `templates/Dockerfile.j2`: reorder so the dependency-install `RUN` blocks
  come right after `FROM`/`ARG`/`USER root`, then a new `{{ rest_of_source_dockerfile_text }}`
  splice point, then the `useradd`/config-dir/firewall/`USER agent`/`WORKDIR`/
  `ENTRYPOINT` block moves after that splice point instead of being
  interleaved with the dependency installs.
- Update `docs/usage.md`'s description of the `--dockerfile` flow to explain
  the new splice order, since it changes what a custom Dockerfile can assume
  is/isn't present during its own steps (gains apt-get-installed tools like
  `git`/`curl` for free; still can't assume the `agent` user exists yet).
- Re-run `tests/test_renderers.py`'s `--dockerfile` splicing tests
  (`test_dockerfile_renderer_extends_source_dockerfile`,
  `_overwrites_existing_agent_uid_setup`, `_overwrites_existing_jj_install`,
  `_overwrites_missing_config_mount_targets`, `_removes_source_user_id_setup`)
  — they assert content via `assertIn`/`assertNotIn`, not relative order, so
  should keep passing, but verify explicitly since they're the only guardrails
  around this splicing behavior today.
- No change needed to `render_python_uv_dockerfile`/`render_rust_cargo_dockerfile`
  (the `--python-uv`/`--rust-cargo` generated Dockerfiles) — those don't go
  through `Dockerfile.j2` splicing at all, they write a standalone Dockerfile
  directly.
