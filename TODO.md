# TODO - outstanding items for next release

- Live markdown view for headless jobs seems broken, I now see only JSON lines 

## Security proposals

OpenSpec changes in `openspec/changes/`, mostly ported from the
`sec-fixing-claude` branch. Implement in this order (`/opsx:apply <change>`):
highest risk first, each after the changes it depends on. Each change's design
and tasks hold the details of the findings it covers. Reviewed against main's
Colima runtime, private host locks and per-stage build-proxy `ARG`s on
2026-11-16. Two items that were listed here are now part of a proposal: the
interim host Git `core.fsmonitor=false` (row 2, which also skips hooks on
snapshot commits) and the devcontainer hidden-VCS masks in `/tmp` (row 6,
task 4.4).

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

## Decided, ready to implement (2026-11-16)

- **Headless prompts that start with `-`.** The entrypoint passes the prompt as
  one argv element (`codex exec`, `opencode run` positionally; Claude, Pi and
  Copilot via `-p "$PROMPT"`), so a prompt beginning with `-` can be parsed as a
  flag. Decision: on the host, before staging the prompt file, prepend a single
  space when the prompt starts with `-`. The text reaches the model unchanged
  in meaning, and no CLI parser treats it as an option, so no reliance on
  per-parser `--` handling. Don't trim: prompts like a Markdown list
  (`- fix X`) or `--dry-run is broken` would lose meaning. Unit-test in
  `tests/test_cli.py`; add an e2e smoke test with a `--help` prompt. The
  subcommand case (`codex exec resume`) only applies to a prompt that is
  exactly a subcommand name, since the prompt is a single argument.
- **Colima workspace roots: document, don't extend the deny list.**
  `colima_profile.CREDENTIAL_DIRS` stays as is. In the Colima docs, recommend a
  dedicated workspace directory and name roots to avoid because they would be
  exported read-write: `~/Library` (Keychains, browser cookies), `~/.colima` /
  `~/.lima` (the VM's own config and SSH keys), `~/.kube`, and `~/.config`.

## Remaining fixes

Not covered by any proposal above. Longer-term hardening (bounded lock waits,
supply chain, mount encoding), VCS and sidecar follow-ups, e2e speed and the
Podman shared-folder issue are in ROADMAP.md.

- **Firewall startup.** Rules are flushed before the final DROP policies go in,
  and connections established before initialization survive it
  (`test_startup_connection_and_agent_icmp_capabilities`). Install policy
  atomically (`iptables-restore`), test failures during setup, and audit the
  outbound ICMPv6 types that stay allowed.
- **Sidecar private-address denials.** An uncached denied CONNECT can send
  `200` before the reset (XFAIL `test_mixed_dns_rebinding_mapped_ipv6_and_nat64`);
  no upstream bytes flow, but the client sees a success line. The deployment
  NAT64 path is untested without `--nat64-origin`.
- **Apple setup notice.** "Local service forwarding with Apple container
  requires this one-time setup" is printed on every Apple session that forwards
  a local service, even when the alias works. Print it only when the alias check
  fails, or reword it as a hint.
