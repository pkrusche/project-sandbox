## 1. Documentation

- [ ] 1.1 Add `docs/agent-proxy.md` treating
      `pkrusche/agentgateway-locally` as a prerequisite. Link to its setup,
      security, backend, and request-log guidance; document `run.py setup`,
      user-managed `run.py up`/`down`, LLM port 4000 versus MCP port 3000, and
      the exact pi/OpenCode invocation without copying its gateway YAML.
- [ ] 1.2 Document loading the gateway key into a host environment variable
      using `run.py key`, explain why the value must not be placed in argv, and
      document key rotation/troubleshooting for 401/403 responses.
- [ ] 1.3 Update `docs/security.md`: distinguish provider credential isolation
      from local-spend authorization; state that the agent VM receives the
      gateway key; explain loopback/vmnet exposure, mandatory strict listener
      auth, private staging/redaction, and external SQLite prompt/response logs.

## 2. CLI surface, credential policy, and preflight

- [ ] 2.1 Add `--agent-proxy URL`, `--agent-proxy-key-env NAME`, and repeatable
      `--agent-proxy-model ID` flags to `cli.py` with help pointing to
      `docs/agent-proxy.md`.
- [ ] 2.2 Validate before container work: supported agent (`pi` or `opencode`),
      HTTP loopback URL with explicit port, at least one model, valid gateway
      key environment-variable name, mutual exclusion with `--pi-ollama`, and
      rejection of `--api-key-env` / `--api-key-env-file`.
- [ ] 2.3 Force `--no-forward-credentials` behavior in proxy mode, purge stale
      staged credentials, and prove that no host OAuth file or provider key is
      mounted or injected. Admit only the dedicated gateway key.
- [ ] 2.4 Read the gateway key only for a real run; reject missing/empty values;
      keep it out of argv, logs, exceptions, transcript output, and dry-run;
      stage secret-bearing provider config with private permissions and cleanup.
- [ ] 2.5 Add authenticated `GET <proxy-base>/models` preflight with a bounded
      timeout and bearer header. Validate JSON/model aliases and emit distinct,
      actionable errors for connection failure, 401/403, malformed response,
      and missing models. Skip it under `--dry-run`.
- [ ] 2.6 Extend dry-run to preview sanitized provider configuration and planned
      forwarding/container commands without reading the key variable, contacting
      the proxy, writing files, or starting resources.
- [ ] 2.7 Add `tests/test_cli.py` coverage for absent-flag no-op, agent/URL/model
      validation, flag conflicts, forced credential exclusion, missing key,
      authenticated preflight outcomes, model validation, redaction, and dry-run
      no-read/no-network/no-write behavior.

## 3. Reachability and firewall

- [ ] 3.1 Generalize `ollama_network.py` forwarding selection to a configurable
      port and internal hostname while keeping `--pi-ollama` output and behavior
      unchanged. Preserve scheme, explicit port, and `/v1` when constructing the
      in-container proxy URL.
- [ ] 3.2 Use a dedicated internal hostname such as
      `agent-proxy.project-sandbox.internal`; preserve Apple localhost-DNS's
      exact-admin-command/never-sudo behavior and all runtime safety checks.
- [ ] 3.3 Extend `firewall.render()` / `init-firewall.sh.j2` with a port-scoped
      allow rule and hostname pin for proxy sessions only. Reuse the Ollama-style
      `--no-firewall` warning.
- [ ] 3.4 Test forwarding selection for non-default ports and paths, proxy-only
      firewall output, unchanged Ollama rendering, unsafe endpoint rejection,
      and no-firewall warnings.

## 4. Agent provider configuration

- [ ] 4.1 Pi: render a private proxy `models.json` with OpenAI-compatible API
      mode, forwarded `/v1` URL, requested model aliases, and gateway key; render
      `settings.json` selecting the provider and first model.
- [ ] 4.2 OpenCode: render a private `opencode.json` custom provider with the
      forwarded `/v1` URL, requested models, and gateway key, compatible with
      existing `--model <provider>/<model>` dispatch.
- [ ] 4.3 Mount generated secret-bearing config only for the selected agent and
      remove it through the existing staged-credential cleanup lifecycle.
- [ ] 4.4 Add renderer/mount tests proving both agents receive the gateway URL,
      requested models, and gateway key; no provider key/OAuth state is present;
      non-proxy behavior is unchanged; serialized diagnostics are redacted.

## 5. User-executable end-to-end checker

- [ ] 5.1 Add stdlib-only `scripts/check-agent-proxy.py`. Accept an
      `agentgateway-locally` checkout path plus pi/OpenCode model overrides;
      validate the checkout and local `project-sandbox` command without changing
      gateway state.
- [ ] 5.2 Have the script invoke the external `pass agentgateway-api-key` with captured output
      and perform an authenticated, bounded `/v1/models` request. Fail clearly
      when the proxy is down, auth is rejected, the response is malformed, or a
      requested model is absent; never print the gateway key.
- [ ] 5.3 Create a temporary project and run a real headless
      `project-sandbox --agent pi` session through the proxy with a minimal
      prompt requiring a unique exact success marker. Report failure for a
      nonzero exit, timeout, or missing marker.
- [ ] 5.4 Run the equivalent headless OpenCode session with a different marker,
      passing the gateway key only through the dedicated host environment
      variable. Report per-agent results and exit nonzero unless both pass.
- [ ] 5.5 Ensure the script warns that it makes two billable LLM requests,
      cleans up its temporary project, does not start/stop/reconfigure the
      gateway, redacts secret-bearing output, and supports actionable timeouts.
- [ ] 5.6 Add unit tests for the checker with mocked HTTP and subprocess
      boundaries: proxy-down/auth/model failures, pi failure, OpenCode failure,
      marker validation, redaction, cleanup, and the two-agent success path.

## 6. Manual integration verification

- [ ] 6.1 Follow `docs/agent-proxy.md` verbatim against the referenced setup and
      run `scripts/check-agent-proxy.py`. Confirm its authenticated proxy check,
      headless pi run, and headless OpenCode run all pass.
- [ ] 6.2 Inspect the agent VMs/staging plan: the gateway key and proxy URL are
      present only where required; OpenAI/Anthropic keys and host OAuth state are
      absent; dry-run and transcripts contain no gateway key.
- [ ] 6.3 Stop the proxy and confirm a new session and the checker both fail at
      authenticated preflight without starting an agent container. Restart it,
      rotate the gateway key, and confirm the old key is rejected and the new
      key succeeds.
