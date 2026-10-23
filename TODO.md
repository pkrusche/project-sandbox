# TODO - outstanding items for next release

- Consider authenticated Internet proxies without exposing credentials or forwarding gateway keys; this release intentionally rejects URL credentials and has no authentication negotiation.
- Consider privileged DNS refresh for long sessions; current endpoint pins require a session restart after DNS changes.
- Run the opt-in `proxy-endpoints` suite on Docker, Podman, and Apple container, including its native IPv6 scenario. Separately verify pinned bundled Pi/OpenCode versions, DNS-name pinning, and IPv6-disabled fallback. The implementation environment has no container runtime or network-administration capabilities.
- Run the opt-in `build-proxy` suite on Apple container behind a VPN-only proxy to confirm the host-loopback build proxy forwarder end to end, including an `https://` proxy URL.
- Verify the Apple container `--internet-proxy` host relay end to end behind a VPN-only proxy (HTTP and `https://` URLs), including the in-sandbox reachability check and firewall pinning of `host.docker.internal:PORT`.
- Let concurrent Apple container sessions share one `--internet-proxy` relay; today the fixed, firewall-baked relay port makes a second session using the same proxy fail.
- A remote `--agent-proxy` on Apple container has the same VPN routing gap as `--internet-proxy` had; consider relaying it through the host as well.
- Confirm on Docker BuildKit, Apple container's builder, and Podman/buildah that the undeclared predefined proxy build-args reach `RUN` steps (including apt's env-based HTTPS proxy and `no_proxy`) and are excluded from the layer cache, so a changed proxy URL or forwarder port reuses cached layers.

## Proxy-during-build review follow-up

- [x] Allow immediate sequential runtime relay sessions without weakening listener exclusivity.
- [x] Build and audit distinct blocked/allowed firewall images in the build-proxy E2E harness.
- [x] Audit the runtime-facing Apple relay URL separately from the upstream proxy URL.
- [x] Reconcile proxy routing/TLS/build-CA docs, centralize Apple setup, and shorten the build-network guide.
- [x] Cover sequential reuse, TLS backpressure/hostname failures, and failure/interruption cleanup; stop fixture threads reliably.
- [x] Share URL/certificate validation cases and CLI test setup, and remove obsolete cleanup terminology.
- [ ] Run the fixed-port relay reuse/exclusivity regressions natively on macOS (`uv run pytest -q tests/test_internet_proxy_relay.py`), alongside the Apple acceptance checks above; local validation used Linux sockets.

## jj 0.46 colocated-workspace fix follow-up

- [ ] Free disk space, then rerun the full suite (`uv run pytest -q`); the last run had ~20 unrelated `git init` exit-128 failures caused by ENOSPC.
- [ ] Rerun the `jj-workflow` e2e suite (`scripts/run-e2e-tests.sh`) on Apple container to confirm `--no-colocate` workspaces work in the container.
- [ ] Confirm `test_setup_does_not_colocate_a_git_worktree` fails without the `--no-colocate` fix.
- [ ] Clean up old e2e leftovers under `.project-sandbox/e2e/`, including `jj-e2e.88BjD8-workspaces/e2e-jj-default`.
- [ ] Workspaces created by jj >= 0.46 before this fix are colocated and still break in the container when reused (`--keep-workspace`); document that they must be recreated, or detect and reject/repair them in `jj_workspace.setup`.
