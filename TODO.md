# TODO — outstanding items

> Reviewed 2026-06-16 against the code. Commits f128643 (`--python-uv`),
> ae2d582 (persistent history) and 821a248 (firewall resolvers) landed the bulk
> of three earlier items; the remaining work below is what verification turned
> up as still open.

## `--python-uv`: synthesised Dockerfile does not actually persist the uv cache

The `--python-uv` / `--python VERSION` flags are implemented and tested
(`cli.py:44-58`, `_resolve_build_source` at `cli.py:398-433`, renderer at
`dockerfile.py:204-231`, tests in `tests/test_cli.py` `PythonUvFlagTests`). The
flag, the `3.11` default, the missing-`pyproject.toml`/`uv.lock` warning, and
the `--dockerfile` mutual-exclusion all work. Two correctness gaps remain:

- **Cache-warming step is a no-op at runtime (functional).** The synthesised
  warm step is `RUN --mount=type=cache,target=/opt/uv-cache uv sync --frozen
  --no-install-project` (`dockerfile.py:227`). A BuildKit `type=cache` mount is
  **ephemeral and is not baked into the final image**, so `/opt/uv-cache` is
  empty when the agent runs — which defeats the documented purpose of letting
  the agent run `uv sync` offline behind the firewall. The README pattern
  (`README.md:60-77`) deliberately warms the cache **without** a cache mount and
  then `chown -R 1000:1000 /opt/uv-cache`. The synthesised version should drop
  the `type=cache` mount (so the cache lands in a layer) and add the
  `chown` to UID 1000 — the sandbox agent runs as UID 1000.
- **Filename differs from earlier spec (cosmetic, decide and align).** The file
  is written to `.project-sandbox/Dockerfile.python-uv` (`dockerfile.py:229`),
  not the `.project-sandbox/Dockerfile.base` originally specced. Either is fine;
  pick one and keep the README/spec consistent.

## Persistent history: mounted to the wrong user, and devcontainer not covered

The interactive-vs-unsupervised scoping, the `.project-sandbox/history/`
location, and the gitignore exclusion are in place for the CLI run path
(`cli.py:577-591`, tests in `tests/test_cli.py`). Two gaps remain:

- **Mount targets are `/root/...` but the container runs as `USER agent`.** The
  mounts target `/root/.bash_history` and `/root/.claude/projects`
  (`cli.py:587-590`), but the container user is `agent` (UID 1000, home
  `/home/agent` — `Dockerfile.j2:86`), and Claude's config dir is
  `/home/agent/.claude` (`container_cli.py`). As `agent`, bash writes to
  `/home/agent/.bash_history` and cannot write to root-owned `/root` anyway, so
  the persisted files are mounted where the process never uses them. Targets
  should be `/home/agent/.bash_history` and `/home/agent/.claude/projects`. The
  existing tests assert the (wrong) `/root` strings, so they pass without
  catching this — update them to assert the agent-home targets.
- **Devcontainer is not covered.** The TODO calls for devcontainer *and*
  interactive sessions, but `devcontainer.render(...)` (`cli.py:178-187`) gets
  only the user's `--mount` values; neither `devcontainer.json.j2` nor the
  devcontainer entrypoint touches history. Add the history mounts there too.

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

## Verify `--timeout` actually tears down the apple/container VM

Code is **complete**: `container run` is launched with `start_new_session=True`
(`session.py:38-44`) and on timeout `_terminate_process_group`
(`session.py:60-88`) SIGTERMs then SIGKILLs the whole process group; tests in
`tests/test_session.py` cover the group-signalling logic. This cannot be
verified without apple/container. Outstanding: confirm on an apple/container
host that the guest VM is gone after a timeout. If the VM lingers despite
`--rm` + group kill, give the run a known `--name`/id and `container stop`/`kill`
it explicitly in the timeout path (not currently implemented).
