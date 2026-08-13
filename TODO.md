# TODO - outstanding items

## Machine-readable outputs from session runs

- [ ] Emit a machine-readable session summary (single JSON line on stdout, or
      `--json-summary <path>`): session id, container name, workspace path, agent,
      resulting bookmark + jj change_id, exit code, start/end time. Removes the need
      for sbx-loop to infer session outcome from logs and jj polling.
- [ ] Add a "reuse existing workspace" functionality with a documented, predictable workspace
      path derived from the branch name — so automated callers passing `--keep-workspace` 
      know where to look. Also, workspace path should be in the session summary output.

## Concurrent --branch runs

- [ ] Document (and enforce) a concurrency contract for parallel `--branch` runs:
      serialize `jj workspace add` internally (jj#9314 can corrupt the caller).
- [ ] Guard the `.build-state.json` image fingerprint cache against duplicate
      concurrent first-builds.

## pi agent support improvements

- [ ] Verify/ensure `pi-headless` passes `-a`/`--approve`. Without it pi silently
      ignores project-local `.agents/skills`, exits 0, and produces a plausible
      result with no skill loaded. Currently the highest-risk unknown.
- [ ] Verify `--model` / `--effort` map onto pi's `--model` / `--thinking`, including
      whether `provider/id[:thinking]` strings survive intact.
- [ ] Pass through a pi tool allowlist (`--tools read,grep,find,ls`) so a review job
      can run read-only. A reviewer that can edit will "fix" what it should be judging.
- [ ] Confirm `--api-key-env` reaches pi as a provider env var (`ANTHROPIC_API_KEY`
      etc.) and that pi's `--api-key` flag is never used internally (argv leak).

## P1 — observability & failure taxonomy

- [ ] Surface pi's `--mode json` event stream as the structured session output — this
      is probably the cheapest way to satisfy item 1 for pi, rather than inventing a
      separate contract.
- [ ] Extract token/cost totals from that stream into the session summary, so callers
      can enforce a per-task spend ceiling.
- [ ] Distinguish rate-limit (429) exits from generic agent failure, alongside the
      existing timeout → 124. Rate limits want longer backoff and no retry-count
      increment.
- [ ] Add `project-sandbox sessions list --json` (or equivalent) so a restarting
      orchestrator can detect orphaned/still-running sessions instead of scanning
      `.project-sandbox/sessions/` and guessing.
- [ ] Make the container name deterministic and reported, so orphan detection can
      match against `container ls --format json`.

## P2 — gateway / config

- [ ] Support injecting a custom pi provider config (baseUrl + scoped token) for the
      agentgateway sidecar path, so no real provider key enters the agent VM.
- [ ] Add a build-only / warm-up invocation that builds the image without starting an
      agent, so an orchestrator can pre-build once before launching N parallel jobs.
- [ ] Pin/verify the image's `openspec` version supports `instructions … --json`
      (v1.7.0+), since the whole OpenSpec drive strategy depends on it.
