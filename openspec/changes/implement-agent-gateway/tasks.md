## 1. Docs scaffold

- [ ] 1.1 Add `docs/gateway.md` describing the sidecar, the credential-isolation
      invariant, the macOS-26 requirement, and the CLI surface (fill in
      details as later tasks land; this is the anchor doc referenced by
      `--help` and `docs/security.md`).
- [ ] 1.2 Add a `docs/security.md` section describing gateway mode as the
      strongest available credential-isolation posture and cross-reference
      `--no-forward-credentials`.

## 2. Config generation (no runtime yet)

- [ ] 2.1 Add `src/project_sandbox/templates/agentgateway-config.yaml.j2`
      rendering a `binds → listeners → routes → backends: ai` config with one
      route per enabled provider (`/claude`, `/codex`, `/opencode` prefixes),
      `backendAuth.key` referencing `$ENV_VAR` only.
- [ ] 2.2 Add `src/project_sandbox/agentgateway.py` with a renderer that
      reads the project's `gateway` config block, renders
      `.project-sandbox/gateway/config.yaml` via `templating.py`, and
      validates the result (`yaml.safe_load` + required-key check) before
      returning.
- [ ] 2.3 Implement `secrets.env` partitioning in `agentgateway.py`: parse
      the configured env file (default `~/.config/project-sandbox/secrets.env`,
      overridable), verify its permissions are private, and split it into
      gateway-only secret values vs. agent-visible config — mirroring the
      partitioning helpers already used for `--api-key-env-file` in `cli.py`.
- [ ] 2.4 Add unit tests in `tests/test_renderers.py` for: rendered config has
      no literal secret values, invalid config is rejected before any mount,
      and permission-check rejection of a non-private secrets file.

## 3. CLI surface

- [ ] 3.1 Add `--gateway[=auto|on|off]`, `--gateway-image`,
      `--gateway-provider` (repeatable), `--gateway-env-file`,
      `--gateway-config` to `build_parser()` in `cli.py`, following the
      existing flag-help conventions.
- [ ] 3.2 Resolve `auto` against `.project-sandbox/gateway/` presence; wire
      the resolved on/off value through `main()` the same way
      `_pi_ollama_enabled` is threaded today.
- [ ] 3.3 When gateway mode resolves to on, force
      `forward_credentials = False` regardless of `--no-forward-credentials`,
      and raise a clear `SystemExit` if the user also passed a flag that
      would stage or forward a credential (`--api-key-env`,
      `--api-key-env-file`, explicit credential forwarding) — implements the
      "mutually exclusive" and preflight-tripwire spec scenarios.
- [ ] 3.4 Add a preflight assertion (before any container starts) that no
      known credential path is staged into the agent home when gateway mode
      is on; fail closed if found.
- [ ] 3.5 Wire `--dry-run --gateway on` to print the rendered config and the
      network/run argv (task 4) with every secret value redacted as
      `<redacted>`, writing nothing — extend the existing `_dry_run()` path.
- [ ] 3.6 Add CLI-level tests in `tests/test_cli.py` for: `--gateway` absent
      is a no-op, `auto` resolution both ways, conflicting-flag rejection,
      dry-run redaction and no-write behavior.

## 4. Apple `container` sidecar orchestration

- [ ] 4.1 Add `src/project_sandbox/gateway_network.py` modeled on
      `ollama_network.py`'s `ForwardingPlan`/`prepare()` shape: a lifecycle
      object that creates a per-project network
      (`container network create --subnet ... gateway-net-<projectid>`),
      starts the gateway container, discovers its IP via
      `container inspect gateway-<projectid> | jq
      '.[0].networks[0].ipv4Address'` (strip the `/<prefix>` suffix), and
      health-checks the listener (TCP connect, bounded retry/timeout).
- [ ] 4.2 Add `build_gateway_run_argv()` (or equivalent) to `container_cli.py`
      building the `container run -d --network ... --mount ...
      <image> -f /config.yaml` argv, mounting the rendered config and the
      partitioned gateway secrets read-only.
- [ ] 4.3 Hard-fail with a named-platform error when gateway mode is
      requested on a runtime/OS that can't do inter-container networking
      (Apple `container` below macOS 26, and the `chroot` runtime) — before
      creating any network or container.
- [ ] 4.4 Sequence orchestration in `cli.py`/`_build_session_command()`:
      render config → partition secrets → create network → start gateway →
      health-check gate → start agent VM with gateway address templated in.
- [ ] 4.5 Implement teardown: stop the gateway container and remove the
      per-project network on normal exit, interruption, or agent VM start
      failure; ensure a failed health check also cleans up any
      already-created gateway resources.
- [ ] 4.6 Add tests mocking `shutil.which` / subprocess calls for: network
      create/run/inspect/teardown argv construction, IP-parsing from the
      documented `container inspect` JSON shape, health-check timeout
      aborting before the agent VM starts, and platform-gate rejection.

## 5. Firewall collapse

- [ ] 5.1 Extend `firewall.render()` and `init-firewall.sh.j2` with a
      `gateway_ip`/`gateway_port` parameter that, when set, replaces the
      normal domain allowlist with a single `ACCEPT` rule scoped to that
      IP:port and default DROP otherwise (both the direct and devcontainer
      variants, matching the existing dual-render pattern).
- [ ] 5.2 Template the discovered gateway IP into `init-firewall.sh` at agent
      VM start time (after task 4's IP discovery).
- [ ] 5.3 Add `tests/test_renderers.py` coverage: gateway mode produces a
      single-destination allow rule and default DROP for everything else;
      non-gateway sessions are unaffected.

## 6. Agent wiring

- [ ] 6.1 Claude: extend `config_agents.py` / `_claude_settings_json()` (or
      the equivalent code path) to set `ANTHROPIC_BASE_URL` to the gateway's
      `/claude` route and `ANTHROPIC_AUTH_TOKEN` to a sentinel value when
      gateway mode is on, in the `env` block of generated
      `.project-sandbox/claude/settings.json`.
- [ ] 6.2 Codex: extend `_codex_config_toml()` to emit a
      `[model_providers.<id>]` entry with `base_url` pointing at the
      gateway's `/codex` route, `wire_api`, and an `env_key` resolving to the
      sentinel, when gateway mode is on.
- [ ] 6.3 OpenCode: add the equivalent generated custom-provider config with
      a gateway `baseURL` and sentinel credential.
- [ ] 6.4 Copilot CLI: when gateway mode is on and Copilot is selected, print
      a clear warning that GitHub-hosted Copilot models cannot be routed
      through the gateway (upstream limitation) instead of silently
      proceeding — reuse the existing `_warn_byok_provider_allowlist`-style
      warning helper pattern in `cli.py`.
- [ ] 6.5 Add tests covering: generated Claude/Codex/OpenCode config contains
      gateway base URL + sentinel and no real key; Copilot-with-gateway
      triggers the warning.

## 7. Integration verification

- [ ] 7.1 On a macOS 26 host, run `--gateway on --agent claude` end-to-end:
      confirm both containers start, the agent VM reaches the gateway,
      direct egress to `api.anthropic.com` from the agent VM is dropped, and
      `env | grep -i ANTHROPIC_API_KEY` inside the agent VM shows only the
      sentinel.
- [ ] 7.2 Kill the gateway mid-session and confirm the agent VM fails closed
      (no direct egress) rather than falling back to a direct connection.
- [ ] 7.3 Confirm macOS 15 (or non-Apple-container runtime) rejects
      `--gateway on` before any container starts.
