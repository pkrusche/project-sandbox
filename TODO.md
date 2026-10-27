# TODO - outstanding items for next release

- A SIGKILLed sandbox CLI still leaves its headless container running until the
  next `scripts/run-e2e-tests.sh` reaps it (socat forwarders die with the CLI
  via `PR_SET_PDEATHSIG`; SIGTERM/SIGHUP clean up both). Consider offering the
  same orphan reaping outside e2e, e.g. via `project-sandbox doctor`.
