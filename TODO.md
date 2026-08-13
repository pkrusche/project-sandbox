# TODO - outstanding items

## pi agent support improvements

- [ ] Verify/ensure `pi-headless` passes `-a`/`--approve`. Without it pi silently
      ignores project-local `.agents/skills`, exits 0, and produces a plausible
      result with no skill loaded.
- [x] Verify `--model` / `--effort` map onto pi's `--model` / `--thinking`, including
      whether `provider/id[:thinking]` strings survive intact.
- [x] Pass through a pi tool allowlist (`--tools read,grep,find,ls`).
- [x] Surface pi's `--mode json` event stream as the structured session output
- [ ] Support injecting a custom pi provider config (baseUrl + scoped token) for the
      agentgateway sidecar path, so no real provider key enters the agent VM.
- [ ] based on the ollama functionality, add support for arbitrary local agent gateway
      forwarding: forward a localhost port into the sandbox via CLI argument.

## Headless output formatting

The intermediate / terminal output in headless sessions currently is in JSON format,
this should rather be formatted as Markdown s.t. a user can follow along more easily.

The JSON session logs will be retained anyway, but we do want a live Markdown translation
for the JSON records.

 - [x] Implement live Markdown for Claude
 - [x] Implement live Markdown for Codex
 - [x] Implement live Markdown for pi
 - [x] Implement live Markdown for Opencode
