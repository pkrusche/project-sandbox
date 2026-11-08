# TODO - outstanding items for next release

- `--build-only --pi-ollama` builds an image without the Ollama firewall rule.
  `--build-only` rejects `--agent`, and the Pi + Ollama setup is only enabled when
  `--agent pi` is also given (`cli.py`, `bool(args.pi_ollama and run_agent == "pi")`).
  A later `--agent pi --pi-ollama --no-build` session then reuses that image, and
  Ollama is refused with "No route to host". Either let `--build-only` honour
  `--pi-ollama` (or accept `--agent`), or have `--no-build` detect an image
  rendered without the session's Ollama settings.
- Manually verify that `kill -9` of the only `--pi-ollama` session releases the
  shared bridge forwarder (it should exit within about one poll interval) and
  that a fresh session then attaches cleanly. The concurrent-session `kill -9`
  case was verified on 2026-11-08 (see docs/development.md).
