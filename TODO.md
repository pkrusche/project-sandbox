# TODO - outstanding items for next release

- A SIGKILLed sandbox CLI still leaves its headless container running until the
  next `scripts/run-e2e-tests.sh` reaps it (socat forwarders die with the CLI
  via `PR_SET_PDEATHSIG`; SIGTERM/SIGHUP clean up both). Consider offering the
  same orphan reaping outside e2e, e.g. via `project-sandbox doctor`.
- On rootless Podman with pasta, a proxy or gateway URL naming the host's own
  LAN address is unreachable from the sandbox (pasta copies that address into
  the container), while the in-container host address `169.254.1.2` is
  meaningless to the CLI's host-side preflight. Map host-own `--internet-proxy`
  and `--agent-proxy` addresses to `169.254.1.2` in the sandbox, as
  `internet_proxy.for_runtime` relays remote proxies on Apple container. That
  would let the build-proxy allowed phase pass on Podman and the
  proxy-endpoints suite stop being skipped there.
