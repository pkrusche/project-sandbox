# TODO — outstanding items

> Reviewed 2026-06-16 against the code. The `--python-uv` and persistent-history
> items have since been fixed (see below); the two remaining items are
> code-complete and only await verification on real hardware.

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
