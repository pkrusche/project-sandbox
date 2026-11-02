# TODO - outstanding items for next release

## GitHub Copilot agent (follow-ups from add-copilot-agent-support)

- Keychain token staging: stage the Copilot token from the macOS Keychain
  (where `copilot login` stores it) as a third source after pass and `gh`.
- Devcontainer support for `--agent copilot`. Needs a token source that does not
  go stale, since a token captured when the devcontainer is generated would
  expire.
- Self-hosted GitHub Enterprise Server (GHES) hosts for `--copilot-host`
  (currently rejected): needs its own host/endpoint model and firewall entries.
- Opt-in GitHub MCP server: a flag to re-enable Copilot's built-in GitHub MCP
  server (now always `--disable-builtin-mcps`) for users who accept that it acts
  on GitHub with the Copilot token.
- Tighten the Copilot transcript renderer and exit reclassification from real
  e2e captures (`scripts/e2e-copilot.py --capture-dir`): replace the synthetic
  JSONL fixtures in `tests/test_transcript.py` with captured ones, and confirm
  the record kinds in `transcript.py` and
  `observability.copilot_json_stream_failure`.
- Re-check the `@github/copilot` pin with `scripts/update-pins.py`: 1.0.98 was
  taken from a web search because the npm registry was unreachable when it was
  added.
