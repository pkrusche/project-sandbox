## 1. Documentation

- [x] 1.1 Add `docs/agent-proxy.md` treating
      `pkrusche/agentgateway-locally` as a prerequisite. Link to its setup,
      security, backend, and request-log guidance; state that project-sandbox
      cannot access its checkout or `run.py`, that gateway lifecycle remains
      user-managed, and that LLM port 4000 differs from MCP port 3000. Show the
      exact pi/OpenCode invocation using regular `--model` without copying its
      gateway YAML.
- [x] 1.2 Document gateway-key precedence: `pass show agentgateway-api-key`,
      then the selected environment variable (default
      `AGENTGATEWAY_API_KEY`), then raw `--agent-proxy-key`. Warn that the CLI
      fallback exposes the key to shell history/process listings even though
      project-sandbox redacts its own output; document rotation and 401/403
      troubleshooting.
- [x] 1.3 Update `docs/security.md`: distinguish provider credential isolation
      from local-spend authorization; state that the agent VM receives the
      gateway key; explain loopback/vmnet exposure, mandatory strict listener
      auth, private staging/redaction, and external SQLite prompt/response logs.

## 2. CLI surface, credential policy, and preflight

- [x] 2.1 Add `--agent-proxy URL`, `--agent-proxy-key-env NAME` (default
      `AGENTGATEWAY_API_KEY`), and raw `--agent-proxy-key KEY` to `cli.py` with
      help pointing to `docs/agent-proxy.md` and warning on the raw-key option;
      reuse existing `--model` and do not add a proxy-specific model-list flag.
- [x] 2.2 Validate before container work: supported agent (`pi`, `opencode`, or
      `bash`),
      HTTP loopback URL with explicit port, required regular `--model`, valid
      gateway key environment-variable name, mutual exclusion with
      `--pi-ollama`, and rejection of `--api-key-env` /
      `--api-key-env-file`. Accept `<model-id>` for pi and require
      `agent-proxy/<model-id>` for OpenCode.
- [x] 2.3 Force `--no-forward-credentials` behavior in proxy mode, purge stale
      staged credentials, and prove that no host OAuth file or provider key is
      mounted or injected. Admit only the dedicated gateway key.
- [x] 2.4 Resolve the gateway key only for a real run: first capture
      `pass show agentgateway-api-key`, then use the selected environment
      variable, then raw `--agent-proxy-key`, and fail clearly if all are
      unavailable. Keep the resolved value out of logs, exceptions, transcript,
      and dry-run output; warn that the raw fallback is already visible in argv;
      stage secret-bearing provider config with private permissions and cleanup.
- [x] 2.5 Add authenticated `GET <proxy-base>/models` discovery with a bounded
      timeout and bearer header. Parse every returned model ID, reject malformed
      or empty `data[].id` values/catalogs, preserve IDs exactly, deduplicate
      while preserving response order, and validate the regular `--model`
      selection. Emit distinct errors for
      connection failure, 401/403, invalid catalog, and missing selected model.
      Skip network discovery under `--dry-run`.
- [x] 2.6 Extend dry-run to preview sanitized provider configuration and planned
      forwarding/container commands without invoking pass, reading the key
      environment variable, contacting the proxy, writing files, or starting
      resources; redact a supplied raw CLI key and explicitly mark model
      discovery and provider generation as deferred.
- [x] 2.7 Add `tests/test_cli.py` coverage for absent-flag no-op, agent/URL/model
      validation, flag conflicts, forced credential exclusion, pass-first
      precedence, environment and raw-CLI fallbacks, argv warning/redaction,
      total key-source failure, authenticated
      discovery outcomes, complete/deduplicated model catalogs, pi/OpenCode
      `--model` normalization and validation, redaction, and dry-run
      no-read/no-network/no-write behavior.

## 3. Reachability and firewall

- [x] 3.1 Generalize `ollama_network.py` forwarding selection to a configurable
      port and internal hostname while keeping `--pi-ollama` output and behavior
      unchanged. Preserve scheme, explicit port, and `/v1` when constructing the
      in-container proxy URL.
- [x] 3.2 Use a dedicated internal hostname such as
      `agent-proxy.project-sandbox.internal` where the runtime permits it; use
      the configured `host.docker.internal` alias on Apple `container` and
      preserve localhost-DNS's exact-admin-command/never-sudo behavior,
      restart/network-connectivity warning, and all runtime safety checks.
- [x] 3.3 Extend `firewall.render()` / `init-firewall.sh.j2` with a port-scoped
      allow rule and hostname pin for proxy sessions only, and probe through
      the final deny-by-default rules. Reuse the Ollama-style `--no-firewall`
      warning.
- [x] 3.4 Test forwarding selection for non-default ports and paths, proxy-only
      firewall output, unchanged Ollama rendering, unsafe endpoint rejection,
      and no-firewall warnings.
- [x] 3.5 Make proxy-mode egress gateway-only by omitting default provider
      domains and the devcontainer host-gateway exception. Preserve only
      explicit `--extra-domain` / `--allow-github` additions, disable inferred
      Bash/Copilot widening, and add renderer and CLI regression tests.

## 4. Agent provider configuration

- [x] 4.1 Pi: render a private proxy `models.json` with OpenAI-compatible API
      mode, forwarded `/v1` URL, every discovered model, and gateway key; render
      `settings.json` selecting the provider without choosing a default model,
      and let the existing `--model <model-id>` dispatch select the run model.
- [x] 4.2 OpenCode: render a private `opencode.json` custom provider named
      `agent-proxy` with the forwarded `/v1` URL, every discovered model, and
      gateway key; select the run model through existing
      `--model agent-proxy/<model-id>` dispatch.
- [x] 4.3 Mount generated secret-bearing config only for the selected agent and
      remove it through the existing staged-credential cleanup lifecycle.
- [x] 4.4 Add renderer/mount tests proving both agents receive the gateway URL,
      complete discovered catalog, and gateway key; discovery order is stable;
      no provider key/OAuth state is present; regular `--model` is passed
      unchanged to each agent; non-proxy behavior is unchanged; serialized
      diagnostics are redacted.
- [x] 4.5 Support interactive and headless Bash by injecting the forwarded URL,
      gateway key, and selected model as `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and
      `OPENAI_MODEL`; keep the key out of argv, clean Apple's staged env file,
      and add CLI regression tests for both modes.
- [x] 4.6 In Bash proxy mode, render and mount both Pi and OpenCode provider
      configs with the forwarded URL, discovered catalog, gateway key, and
      selected default model; preserve secret cleanup and add renderer/session
      regression tests.

## 5. User-executable end-to-end checker

- [x] 5.1 Add stdlib-only `scripts/check-agent-proxy.py`. Accept an
      proxy URL and pi/OpenCode model selections, defaulting both to
      `gpt-5-mini` with an `AGENT_PROXY_TEST_MODEL` override; require no external
      checkout and validate the local `project-sandbox` command without changing
      gateway state.
- [x] 5.2 Resolve the checker key from `pass show agentgateway-api-key`, then its
      environment fallback, then a raw checker CLI option. Perform authenticated,
      bounded `/v1/models` discovery. Fail clearly when no key is available, the
      proxy is down, auth is rejected, the catalog is malformed/empty, or either
      selected model is absent; never print the gateway key and warn on raw argv.
- [x] 5.3 Create a temporary project and run a real headless
      `project-sandbox --agent pi --model <model-id>` session through the proxy
      with a minimal prompt requiring a unique exact success marker. Report
      failure for a nonzero exit, timeout, or missing marker.
- [x] 5.4 Run the equivalent headless OpenCode session with
      `--model agent-proxy/<model-id>` and a different marker, passing the
      resolved gateway key to child project-sandbox only through the dedicated
      host environment variable. Report per-agent results and exit nonzero
      unless both pass.
- [x] 5.5 Ensure the script warns that it makes two billable LLM requests,
      cleans up its temporary project, does not start/stop/reconfigure the
      gateway, redacts secret-bearing output, and supports actionable timeouts.
- [x] 5.6 Add unit tests for the checker with mocked HTTP and subprocess
      boundaries: proxy-down/auth/model failures, pi failure, OpenCode failure,
      marker validation, redaction, cleanup, and the two-agent success path.

## 6. Manual integration verification

- [x] 6.1 Follow `docs/agent-proxy.md` verbatim against the referenced setup and
      run `scripts/check-agent-proxy.py`. Confirm its authenticated proxy check,
      headless pi run, and headless OpenCode run all pass.
- [x] 6.2 Inspect the agent VMs/staging plan: the gateway key and proxy URL are
      present only where required; OpenAI/Anthropic keys and host OAuth state are
      absent; dry-run and transcripts contain no gateway key.
- [x] 6.3 Stop the proxy and confirm a new session and the checker both fail at
      authenticated preflight without starting an agent container. Restart it,
      rotate the gateway key, and confirm the old key is rejected and the new
      key succeeds.
