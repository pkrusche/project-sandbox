## 1. Documentation

- [ ] 1.1 Add `docs/agent-proxy.md`: what the feature does (credential
      isolation via a user-managed local proxy), how to install, configure,
      and start `agentgateway` locally with a worked config example
      (loopback-bound listener, per-provider routes, credentials referenced
      from the proxy's environment, pinned version the example was verified
      against), listener-must-not-require-client-auth note, and the CLI
      usage for pi and OpenCode (`--agent-proxy`, `--agent-proxy-model`).
- [ ] 1.2 Add a `docs/security.md` section describing the isolation posture
      (agent VM holds only a sentinel), its boundaries (loopback proxy is
      reachable by any local process; `project-sandbox` does not verify the
      proxy's config or credential handling), and cross-reference
      `--no-forward-credentials`.

## 2. CLI surface and validation

- [ ] 2.1 Add `--agent-proxy URL` and repeatable `--agent-proxy-model ID`
      to `build_parser()` in `cli.py`, following existing flag-help
      conventions and referencing `docs/agent-proxy.md`.
- [ ] 2.2 Validate in `main()` before any container work: loopback-only URL
      (parse and reject non-loopback hosts), supported agent (`pi` /
      `opencode` only), at least one `--agent-proxy-model`, mutual exclusion
      with `--pi-ollama`, and rejection of `--api-key-env` /
      `--api-key-env-file`.
- [ ] 2.3 Force `--no-forward-credentials` behavior when `--agent-proxy` is
      set (no staging, mounting, or forwarding; previously staged
      credentials removed), threading the resolved state the same way
      `_pi_ollama_enabled` is threaded today.
- [ ] 2.4 Add the preflight bounded TCP reachability check against the proxy
      URL (skipped under `--dry-run`), aborting with an error that names the
      URL and points at `docs/agent-proxy.md`.
- [ ] 2.5 Extend `_dry_run()` so `--dry-run --agent-proxy ...` prints the
      baked provider config and planned commands, writes nothing, and skips
      the reachability check.
- [ ] 2.6 Add `tests/test_cli.py` coverage: flag absent is a no-op,
      unsupported-agent rejection, non-loopback rejection, missing-model
      rejection, `--pi-ollama` conflict, credential-flag conflict, forced
      no-forward-credentials, dry-run no-write/no-check behavior.

## 3. Reachability and firewall

- [ ] 3.1 Generalize `ollama_network.py`'s forwarding-strategy selection to
      a configurable port and internal hostname (shared helper or
      parametrized `prepare()`), keeping `--pi-ollama` behavior unchanged;
      use a dedicated hostname for the proxy (e.g.
      `agent-proxy.project-sandbox.internal`) and preserve the
      Apple-localhost-DNS "print the exact admin command, never sudo"
      behavior for the new hostname.
- [ ] 3.2 Extend `firewall.render()` / `init-firewall.sh.j2` with a proxy
      endpoint parameter producing a port-scoped ACCEPT rule and the
      hostname pin, following the existing `pi_ollama` pattern in both
      rendered variants.
- [ ] 3.3 Reuse the `--no-firewall` warning pattern
      (`_warn_pi_ollama_no_firewall`) for the proxy hostname.
- [ ] 3.4 Add tests: forwarding-plan selection for the proxy port per
      runtime, firewall render includes the endpoint rule only in proxy
      sessions, unchanged `--pi-ollama` rendering (regression), and the
      no-firewall warning.

## 4. Agent provider configuration

- [ ] 4.1 pi: extend `config_agents.render()` to bake a proxy provider
      `models.json` (proxy base URL, configured models, sentinel `apiKey`)
      and `settings.json` (`defaultProvider`, first model as
      `defaultModel`), mirroring the `_pi_ollama_*` helpers; decide the
      sentinel value form (static vs. per-session random) here.
- [ ] 4.2 OpenCode: generate an `opencode.json` custom provider block
      (proxy `baseURL`, configured models, sentinel key) staged into the
      container, compatible with `--model <provider>/<model>` selection.
- [ ] 4.3 Add `tests/test_renderers.py` coverage: generated pi and OpenCode
      configs contain the proxy base URL, all configured models, a sentinel,
      and no real key; non-proxy rendering is unchanged.

## 5. Integration verification (manual)

- [ ] 5.1 Following `docs/agent-proxy.md` verbatim, start `agentgateway`
      locally and run `--agent pi --agent-proxy ... --agent-proxy-model ...`
      end-to-end: the agent completes a prompt through the proxy, and
      inspecting the agent VM shows only the sentinel and base URL, no real
      credential.
- [ ] 5.2 Repeat with `--agent opencode`.
- [ ] 5.3 Stop the proxy and confirm: a new session aborts at preflight with
      the documented error; killing it mid-session makes agent requests fail
      without any credential fallback.
