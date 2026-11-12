# TODO - outstanding items for next release

## Security proposals

OpenSpec changes in `openspec/changes/`, mostly ported from the
`sec-fixing-claude` branch. Implement in this order (`/opsx:apply <change>`):
highest risk first, each after the changes it depends on. Each change's design
and tasks hold the details of the findings it covers.

| # | Change | Covers | Depends on |
| --- | --- | --- | --- |
| 1 | `derive-vcs-mounts-from-host` | **High:** agent-written jj/Git pointer files choose mount targets (e.g. over `/usr/local/bin`) and host sources (any host directory) for the next `--branch` session | — |
| 2 | `harden-host-command-execution` | **High:** host capture runs agent-planted Git hooks/fsmonitor and jj signing/fsmonitor programs; host tool lookup and cwd; `doctor` jj config-id check | — |
| 3 | `isolated-vcs-access` | New opt-in `--vcs-access isolated` for Git and jj: no host metadata in the container, one host snapshot commit | 2 |
| 4 | `protect-host-executed-project-config` | `full` mode: `.git/hooks`, `.git/config` (incl. config-defined filters), jj config pointers; config-change report | 2 |
| 5 | `validate-all-mount-sources` | Protected and credential `--mount` sources in every egress mode; protected targets; symlink escapes | — |
| 6 | `minimal-devcontainer` | Devcontainer gateway rule; offline, agent-free devcontainer; always-masked `.devcontainer/` | — |
| 7 | `mount-session-firewall` | Firewall as a per-session mount; stale firewall under `--no-build`; policy changes no longer rebuild images | 6 |
| 8 | `scope-firewall-allowlist` | Firewall-mode identity and ports, private addresses in pins, GitHub/Copilot breadth, per-agent providers | 7 |
| 9 | `sidecar-policy-hardening` | Sidecar plain HTTP, wildcard apex and public suffixes, GitHub host list (`lfs.github.com`), Copilot methods/paths, configurable providers, tunnel lifetime and `--terminate`, upstream warning, log bounds | 8 |
| 10 | `verify-image-inputs` | Image input labels; `--no-build` verification | 7 |
| 11 | `read-only-agent-history` | Claude history read-only by default | — |

## Remaining fixes

Not covered by any proposal above. Longer-term hardening (lock-file checks and
bounded waits, supply chain, mount encoding), VCS and sidecar follow-ups, e2e
speed and the Podman shared-folder issue are in ROADMAP.md.

- **Firewall startup.** Rules are flushed before the final DROP policies go in,
  and connections established before initialization survive it
  (`test_startup_connection_and_agent_icmp_capabilities`). Install policy
  atomically (`iptables-restore`), test failures during setup, and audit the
  outbound ICMPv6 types that stay allowed.
- **Sidecar private-address denials.** An uncached denied CONNECT can send
  `200` before the reset (XFAIL `test_mixed_dns_rebinding_mapped_ipv6_and_nat64`);
  no upstream bytes flow, but the client sees a success line. The deployment
  NAT64 path is untested without `--nat64-origin`.
- **macOS lock-open race.** `host_locks.path_lock` opens with `O_CREAT` once;
  macOS can fail a concurrent `O_CREAT` of one new name with `ENOENT`. Retry the
  open (the `sec-fixing-claude` branch's `paths.open_lock_file`) and add a
  concurrency regression test.
- **Apple setup notice.** "Local service forwarding with Apple container
  requires this one-time setup" is printed on every Apple session that forwards
  a local service, even when the alias works. Print it only when the alias check
  fails, or reword it as a hint.
