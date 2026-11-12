# Roadmap

This file contains functionality researched but not yet planned for execution
in the next release via <TODO.md>. Items planned for the next release are in
TODO.md's "Security proposals" table, with their OpenSpec changes in
`openspec/changes/`.

## VCS isolation and host capture

Planned for the next release (TODO.md): `--vcs-access isolated`
(`isolated-vcs-access`), pointer-independent metadata mounts
(`derive-vcs-mounts-from-host`), hooks/fsmonitor-free host capture
(`harden-host-command-execution`) and restored Git/jj config in `full` mode
(`protect-host-executed-project-config`). What remains after them:

- **Default mode.** `--vcs-access full` stays the default. Once isolated mode
  has been used in practice, evaluate making it (or `read-only`) the default,
  with an explicit opt-in to full access and a migration note for agents that
  commit inside the sandbox.
- **Filters selected by tracked files.** A tracked `.gitattributes` can route
  files through a filter or diff driver configured in the user's trusted global
  config during host `git add`. `protect-host-executed-project-config` restores
  repository config but cannot stop global drivers; consider neutralizing
  filters during capture.
- **Isolated-mode follow-ups** (deferred by `isolated-vcs-access`):
  - `--share PATH[:rw]` to bring ignored folders (data, `node_modules`, `.env`
    directories) into an isolated session, read-only by default. The
    `sec-fixing-claude` branch's `vcs-session-modes` design has the checks.
  - A private per-session copy (`--copy`) as an alternative to sharing.
  - Keeping the agent's commit series instead of one snapshot. That needs the
    host to read a repository the agent wrote (bundle or fetch from an
    untrusted local repository), which is why isolated mode doesn't.
  - Measure setup time on a large repository (setup scans the tree twice).
- **Concurrent writes to a shared store.** `full`, `read-only` and `hidden`
  `--branch` sessions still mount the main `.jj/repo` store (and the Git
  backend) into every container. Concurrent writes from separate VMs (Apple
  `container` with VirtioFS) may not keep lock-file and rename atomicity.
  Isolated mode avoids the shared store; for the other modes, consider a
  per-session clone merged back at teardown. The host-side `_workspace_lock`
  serializes setup and finalization only.

See [VCS security](docs/security.md#vcs-metadata-access) and
[VCS access limitations](docs/vcs-access.md#scope-and-remaining-risks).

## Egress sidecar follow-ups

- **Per-sandbox denial attribution** (egress-sidecar-macos). Sandboxes reach the
  sidecar through the runtime's port forwarder, so Squid's `client` address does
  not identify a sandbox, and the session-end summary counts the denials of
  every client of the sidecar during the session's window. Attribution needs a
  per-session identity on the proxy connection, for example a per-session
  listener or proxy credential. Per-session policies would build on the same
  identity.
- **Upstream proxy authentication** (egress-sidecar-macos). Basic, NTLM or
  Kerberos to the `[egress.upstream]` parent. Today a 407 from the parent fails
  the request.
- **Business and enterprise Copilot hosts** (egress-sidecar-macos). Capture them
  with `scripts/e2e-copilot-egress.py`: `api.{business,enterprise}.githubcopilot.com`
  are admitted by analogy with the individual-plan capture (egress-sidecar-macos
  design.md Decision 5).
- **GHE.com tenants for `--copilot-host` in sidecar mode** (rejected today).
  Capture the tenant host set and add a tenant profile. `scope-firewall-allowlist`
  keeps tenants working in firewall mode behind `--copilot-tenant-firewall`;
  remove that flag when the tenant profile exists.

## GitHub Copilot agent follow-ups

Deferred by `add-copilot-agent-support`:

- **Self-hosted GitHub Enterprise Server (GHES) hosts for `--copilot-host`**
  (currently rejected): needs its own host/endpoint model and firewall entries.
- **Transcript renderer from real captures.** Tighten the Copilot transcript
  renderer and exit reclassification from real e2e captures
  (`scripts/e2e-copilot.py --capture-dir`): replace the synthetic JSONL fixtures
  in `tests/test_transcript.py` with captured ones, and confirm the record kinds
  in `transcript.py` and `observability.copilot_json_stream_failure`.

## Remote proxy endpoints follow-ups

Remote HTTP/HTTPS proxy and gateway endpoints are implemented; see
[network access](docs/networking.md#endpoint-rules) and
[development](docs/development.md#proxy-endpoint-verification). The opt-in
`proxy-endpoints` suite runs on Docker, Podman and Apple container, with
fixtures behind each runtime's host-loopback path. Still to consider:

- Let concurrent Apple container sessions share one `--internet-proxy` relay;
  today the firewall-baked relay port makes a second session using the same
  proxy fail. `mount-session-firewall` removes the baked port, which makes a
  per-session relay port possible.
- Authenticated Internet proxies without exposing credentials or forwarding
  gateway keys; URL credentials are rejected today.
- Privileged DNS refresh for long sessions; endpoint pins currently need a
  session restart after DNS changes.
- A remote `--agent-proxy` on Apple container has the same VPN routing gap as
  `--internet-proxy` had; consider relaying it through the host as well.
- Verification gaps: the native IPv6 scenario, the HTTPS scenario on Apple
  container (its forwarder address changes per run, so it needs
  `--proxy-fixture-host`), pinned Pi/OpenCode versions, DNS-name pinning and
  IPv6-disabled fallback.

## Additional hardening

- **Mount encoding:** CLI bind mounts use CSV encoding, but devcontainer mount
  strings still need consistent runtime encoding. Verify live parse/render round
  trips for unusual project names (commas, quotes, dollars, spaces, wildcards)
  on each runtime, and fail closed for unsupported control characters and mount
  forms. `validate-all-mount-sources` adds source and target rules, not
  encoding.
- **Host lock files:** `host_locks.path_lock()` uses `O_NOFOLLOW` and avoids
  truncation, but does not verify that the opened inode is a regular file owned
  by the caller, and names are predictable in shared temporary storage. Use a
  verified private per-user lock directory, `fstat()` checks, and bounded waits
  with useful diagnostics (XFAIL `test_host_lock_wait_is_bounded`). Never unlink
  a live lock file to recover it, since that can create two independently
  locked inodes.
- **Supply chain:** exact top-level npm versions are useful, but global
  `npm install -g package@version` does not freeze transitive dependency
  resolution (XFAIL `test_installed_tool_dependency_inventory`). Use reviewed
  lockfiles/integrity metadata or reproducible tool layers, and define a
  deliberate image/security-update cadence. Project build scripts and
  user-supplied Dockerfiles remain trusted image-producing code.

## E2E suite speed

On Apple container a fully cached image build still costs about 15 s, because
every build re-exports the whole image; the security suite now shares one image
per egress mode. Further options:

- **Cross-project build reuse in the CLI.** Key the build cache on the inputs
  fingerprint without the tag, and re-tag an existing image on a hit. This would
  also help users with fresh `--branch` worktrees. It changes the image trust
  model, so it builds on `verify-image-inputs`' labels.
- **Merge parametrized sidecar cases.** `test_repository_host_exclusions` runs
  twelve sessions with the same policy; one session probing every host would
  save about a minute per runtime, at the cost of per-case isolation.

## Podman machine shared-folder staleness

Observed 2026-11-11 on macOS with Podman 6.0 (Podman machine VM): the firewall
e2e suite's second container failed with `FileNotFoundError` creating
`/workspace/firewall.ok` after the host had deleted that file following the
first session. Overwriting files that still existed worked. Unconfirmed cause:
the VM's shared-folder (virtiofs) cache still lists the deleted file across
containers. The suite now writes one marker per session, which avoids the case
but does not fix it. Users hit the same problem if they delete a workspace file
on the host between Podman sessions and the agent re-creates it. Reproduce
outside the suite (create a file in one container, delete it on the host,
create it again in a fresh container); if it reproduces, document it in
`docs/` and check whether the machine's cache mode or a mount option avoids it.
