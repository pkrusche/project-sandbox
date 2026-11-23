# TODO - outstanding items for next release

Unimplemented items from before 2026-11-17 are in [ROADMAP.md](ROADMAP.md).

- `scripts/check-agent-proxy.py` passes when its marker appears anywhere in the
  session output. Pi's JSON events echo the prompt, which contains the marker,
  so an empty reply still passes. Check the final assistant message instead, as
  `scripts/e2e-pi-agent-proxy-models.py` does (OpenCode needs its own parser).
- The Pi Ollama `models.json` sets no `contextWindow`/`maxTokens`, so Pi assumes
  128,000/16,384 tokens whatever the model's or server's real `num_ctx` is.
- Agent-proxy model limits and API routing are matched by model ID family in
  `config_agents._PROXY_MODEL_LIMITS`. Re-check them against Pi's catalog when
  bumping the Pi pin or adding gateway model families.
