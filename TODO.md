# TODO - outstanding items

## Machine-readable outputs from session runs

- [x] Emit a machine-readable session summary (single JSON line on stdout, or
      `--json-summary <path>`): session id, container name, workspace path, agent,
      resulting bookmark + jj change_id, exit code, start/end time.
- [x] Add a "reuse existing workspace" functionality with a documented, predictable workspace
      path derived from the branch name — so automated callers passing `--keep-workspace`
      know where to look. Also, workspace path should be in the session summary output.

## Concurrent --branch runs

- [x] Document (and enforce) a concurrency contract for parallel `--branch` runs:
      serialize `jj workspace add` internally (jj#9314 can corrupt the caller) with a
      filesystem based lock.
- [x] Guard the `.build-state.json` image fingerprint cache against duplicate
      concurrent first-builds.

## pi agent support improvements

- [ ] Verify/ensure `pi-headless` passes `-a`/`--approve`. Without it pi silently
      ignores project-local `.agents/skills`, exits 0, and produces a plausible
      result with no skill loaded.
- [ ] Verify `--model` / `--effort` map onto pi's `--model` / `--thinking`, including
      whether `provider/id[:thinking]` strings survive intact.
- [ ] Pass through a pi tool allowlist (`--tools read,grep,find,ls`).
- [ ] Surface pi's `--mode json` event stream as the structured session output
- [ ] Support injecting a custom pi provider config (baseUrl + scoped token) for the
      agentgateway sidecar path, so no real provider key enters the agent VM.
- [ ] based on the ollama functionality, add support for arbitrary local agent gateway
      forwarding: forward a localhost port into the sandbox via CLI argument.

## Observability & Containers

- [x] Distinguish rate-limit (429) exits from generic agent failure, alongside the
      existing timeout → 124. Rate limits want longer backoff and no retry-count
      increment.
- [x] Add `project-sandbox sessions list --json` (or equivalent) so a restarting
      orchestrator can detect orphaned/still-running sessions instead of scanning
      `.project-sandbox/sessions/` and guessing.
- [x] Make the container name deterministic and reported, so orphan detection can
      match against `container ls --format json`.
- [x] Add a build-only / warm-up invocation that builds the image without starting an
      agent, so an orchestrator can pre-build once before launching N parallel jobs.
