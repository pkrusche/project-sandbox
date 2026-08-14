# TODO - outstanding items

## pi agent support improvements

- [ ] Verify/ensure `pi-headless` passes `-a`/`--approve`. Without it pi silently
      ignores project-local `.agents/skills`, exits 0, and produces a plausible
      result with no skill loaded.
- [x] Verify `--model` / `--effort` map onto pi's `--model` / `--thinking`, including
      whether `provider/id[:thinking]` strings survive intact.
- [x] Pass through a pi tool allowlist (opt-in `--pi-tools`, e.g.
      `--pi-tools read,grep,find,ls`).
- [x] Surface pi's `--mode json` event stream as the structured session output
- [ ] Support injecting a custom pi provider config (baseUrl + scoped token) for the
      agentgateway sidecar path, so no real provider key enters the agent VM.
- [ ] based on the ollama functionality, add support for arbitrary local agent gateway
      forwarding: forward a localhost port into the sandbox via CLI argument.
