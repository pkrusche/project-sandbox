# TODO — outstanding items

## Prompt-file mount likely fails on apple/container (same file-mount bug)
- **Where:** `src/project_sandbox/cli.py` — the `--prompt` and `--prompt-text`
  headless paths bind-mount a single *file* (`type=bind,source=<prompt
  file>,target=/workspace/.project-sandbox-prompt,readonly`).
- **Why it matters:** apple/container rejects single-file bind mounts (`path
  '<file>' is not a directory`), the same limitation that broke the bash-history
  mount. This will fail every headless run on apple/container — including
  `scripts/verify-timeout-teardown.sh`, which uses `--prompt-text`.
- **Fix:** persist via a *directory* mount, mirroring the history fix — e.g.
  mount `.project-sandbox/prompts` read-only and point
  `PROJECT_SANDBOX_PROMPT_FILE` at the file inside it; for `--prompt FILE`, copy
  the file into that dir first (or mount its parent). Needs argv-construction
  tests and a docker check.

## Firewall: verify multi-resolver rules on a real iptables host
  
Code is **complete**: `init-firewall.sh.j2` now collects all IPv4/IPv6
nameservers via `mapfile` and emits NAT/ACCEPT rules per resolver (no more
`{print $2; exit}` for DNS), with a `127.0.0.11` fallback; README says
"resolver(s)"; `tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`
covers the rendered script. The unit tests are render-only by policy and do
**not** exercise live iptables. Outstanding: run the rendered script on a host
with iptables (multiple `nameserver` entries in `resolv.conf`) and confirm DNS
to every resolver is permitted and nothing else leaks before treating this as
shipped — it is the network security boundary.

Fixed along the way (2026-06-16, still wants live-iptables verification): the
NAT-preservation step used `iptables-restore --noflush -t nat`, but `-t` is not
an `iptables-restore` option, so it tried to open a file named `nat`
(`Can't open nat: No such file or directory`) and aborted the whole firewall on
apple/container. Rules are now wrapped in a `*nat`/`COMMIT` block and restored
non-fatally, and the `NAT4`/`NAT6` builders only append real matches (the old
bare-newline padding forced a restore on empty input).

## Verify `--timeout` actually tears down the apple/container VM

Code is **complete**: `container run` is launched with `start_new_session=True`
(`session.py:38-44`) and on timeout `_terminate_process_group`
(`session.py:60-88`) SIGTERMs then SIGKILLs the whole process group; tests in
`tests/test_session.py` cover the group-signalling logic. This cannot be
verified without apple/container. Outstanding: run
`scripts/verify-timeout-teardown.sh` on an apple/container host — it times out a
sleeping run and asserts no new container/VM survives (by diffing the running
set before/after). If the VM lingers despite `--rm` + group kill, give the run a
known `--name`/id and `container stop`/`kill` it explicitly in the timeout path
(not currently implemented); the script prints the exact `container stop`
command for any leaked VM it finds.

---

## Done (2026-06-16)

- **`--python-uv` cache persistence** — the synthesised Dockerfile warmed the uv
  cache in an ephemeral `--mount=type=cache` layer, so `/opt/uv-cache` was empty
  at runtime. Now warms with a plain `RUN uv sync ... && chown -R 1000:1000
  /opt/uv-cache` so the cache lands in an image layer owned by the agent user
  (`dockerfile.py`). Filename kept as `Dockerfile.python-uv`.
- **Persistent history mount targets** — history was bind-mounted at `/root/...`
  but the container runs as `USER agent`, so it never took effect. Targets
  corrected to `/home/agent/.bash_history` and `/home/agent/.claude/projects`,
  the host-path setup was factored into `paths.ensure_history_paths`, and the
  same history mounts were added to the devcontainer (`cli.py`, `paths.py`,
  `devcontainer.py`, `templates/devcontainer.json.j2`).
- **Persistent history bash mount failed on apple/container** — root cause:
  apple/container rejects single-*file* bind mounts (`path '<file>' is not a
  directory`), and bash history was mounted as the `~/.bash_history` file. Now
  persisted via a *directory* mount (`.project-sandbox/history/shell` →
  `/home/agent/.bash_history.d`) plus `HISTFILE`, while `claude_projects` stays a
  directory mount (`paths.py`, `cli.py`, `devcontainer.py`,
  `templates/devcontainer.json.j2`). Also fixed the related "sources missing at
  container-create time" gap with a devcontainer `initializeCommand` that
  recreates the directories on the host first.
- **apple/container build context** — `build_image` passed an absolute build
  context, which apple/container does not mount into BuildKit, so every `COPY`
  failed with "<file>: not found". It now builds with `.` as the context and
  `cwd` set to the context dir (`-f` relative), matching Apple's documented
  form; docker/podman unaffected (`container_cli.py`).
