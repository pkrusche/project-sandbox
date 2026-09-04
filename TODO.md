# TODO - outstanding items for next release

## CA-certificate injection

- Add a feature to inject a CA certificate (or multiple certificates) that will
  allow the proxy to inspect traffic / support the use of corporate proxies.

  High-level details:
  - A manual workaround already exists for the `--dockerfile` path: put
    `COPY corporate-ca.crt /usr/local/share/ca-certificates/` +
    `RUN update-ca-certificates` (plus any `ENV HTTPS_PROXY=...`) in a stage
    named `prefix` that the final stage inherits from; `dockerfile.py` inserts
    the sandbox's dependency stage right after it (see `docs/usage.md`,
    `tests/test_renderers.py::test_dockerfile_renderer_inserts_dependencies_after_prefix`).
    There is no equivalent for the plain `--base-image` path, which renders
    `templates/Dockerfile.j2` directly with no hook for extra certs.
  - `docs/internet-proxy.md` currently states `--internet-proxy` "intentionally
    provides no ... TLS interception, CA installation"; proxy traffic is plain
    `http://` end to end (env vars only, see `internet_proxy.py`). This feature
    would reverse that stance, so the doc and the security-boundary description
    need to be updated together with the implementation.
  - `ROADMAP.md`'s "Internet proxy: allow non-loopback proxy hosts" section
    already flags "Figuring out CA certificates & SSL" as blocking work for a
    remote (non-loopback) proxy, since `CONNECT` currently goes out in the
    clear once the hop leaves loopback. That work and this one likely need to
    land together, or this one should stay scoped to loopback.
  - All four bundled agent CLIs (`claude`, `codex`, `opencode`, `pi`) are npm
    packages running under Node (`templates/Dockerfile.j2`); `git`/`jj`/`curl`
    generally trust the OS store via OpenSSL, but Node/npm's own CA handling
    doesn't always follow `update-ca-certificates` without also setting
    `NODE_EXTRA_CA_CERTS` (and possibly npm's own `cafile` config).

  Open questions:
  - Where does the injected cert enter the build: a new `--ca-cert PATH`
    (repeatable, like `--extra-domain`) that `Dockerfile.j2` renders a
    `COPY`+`update-ca-certificates` step for, reuse of the existing `prefix`
    stage convention, or both?
  - Is `update-ca-certificates` (OS trust store) sufficient, or do we also need
    to set `NODE_EXTRA_CA_CERTS`/`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` in the
    container environment for the Node-based agents and any Python tooling?
  - Does this only affect the firewall-allowlisted domains (so MITM'ing our
    own permitted traffic), or is it meant to support an arbitrary corporate
    TLS-inspecting proxy sitting in front of *all* egress — and if the latter,
    how does that interact with the domain/IP allowlist model in
    `init-firewall.sh.j2`, which pins resolved IPs rather than doing DNS at
    request time?
  - Is the cert a build-time input (baked into the image, rebuild required to
    rotate) or a run-time mount (like credentials, refreshed per session)? A
    build-time cert changes the Dockerfile-checksum trust story described in
    `docs/security.md`; a mounted one needs its own read-only mount + masking
    treatment.
  - Should this apply uniformly to `--dockerfile`, `--base-image`, and the
    generated `.devcontainer/`, or start narrower (e.g. base-image + direct
    CLI runs only)?

## Harden the unsupervised modes

- Currently, unsupervised sessions still have access to configs & session logs. 
  This is only really needed in the setting where we work interactively, for
  headless sessions, the environment should only contain the minimum amount of
  information and credentials to execute the task at hand.

  High-level details:
  - Some of this is already done: `ensure_history_paths`'s mounts (shell
    history at `HISTORY_SHELL_TARGET`, and `~/.claude/projects` conversation
    transcripts at `HISTORY_CLAUDE_PROJECTS_TARGET`) are only added `if not
    unsupervised` (`cli.py` around the `_build_session_command` mount
    assembly). `/workspace/.project-sandbox` (which would otherwise expose the
    host's generated config and any host-side `.project-sandbox/sessions/*.log`
    through the workspace mount itself) is masked with an empty read-only
    mount in *every* mode, headless included.
  - What is still unconditional in both modes: the `/project-sandbox-config/<agent>`
    mounts (rendered `settings.json` / `config.toml` / `models.json`, see
    `container_cli.build_mount_specs`) and the `/project-sandbox-secrets/<agent>`
    credential mounts (gated only by `--no-forward-credentials`, not by
    interactive vs. headless).
  - The most concrete leak found: `config_agents._sync_opencode_credentials`
    copies OpenCode's entire `~/.local/share/opencode` and `~/.local/state/opencode`
    directories (no `include_files` filter, unlike codex/pi which stage only
    `auth.json`) into `/project-sandbox-secrets/opencode`, and
    `_provision.sh.j2` copies both wholesale into the container's `$HOME` on
    every run. OpenCode stores per-project session/message history under
    `.local/share/opencode`, so a headless OpenCode run currently gets the
    host user's OpenCode session history across *all* their projects, not just
    this one.
  - By contrast, Claude's staged `.claude.json` is already narrowed to
    `CLAUDE_CREDENTIAL_STATE_KEYS` (`config_agents._stage_config_state`), and
    codex/pi credential sync already passes `include_files=("auth.json",)` —
    so those three are already minimal. OpenCode looks like the outlier.

  Open questions:
  - Is "configs" in scope here the `/project-sandbox-config/<agent>` mount
    (rendered, non-secret settings), or does it mean something the agent
    shouldn't be able to introspect at all in headless mode (e.g. which
    permission-mode profile is active)? These are needed for the agent CLI to
    run at all, so "harden" here probably means "narrow", not "remove".
  - For the OpenCode finding: does headless OpenCode need any of
    `.local/share/opencode` / `.local/state/opencode` to function (e.g. model
    cache, provider registration), or can headless runs skip staging those
    directories entirely, or filter them to just the current project's
    session-scoped files?
  - "Session logs" in the TODO text — does this mean the OpenCode session
    history above, or is there a separate host-side artifact still in mind
    (e.g. `.project-sandbox/sessions/*.log`, `~/.local/state/project-sandbox/sessions/*.json`
    from `observability.py`)? Neither of the latter two appear to be mounted
    into the container today, so confirming the intended target changes the
    fix.
  - Should credential/config mount narrowing key off `unsupervised` (prompt
    present) the same way the history mounts do, or off a broader "headless"
    concept that should also apply to, say, `--agent-proxy` sessions or CI
    invocations run interactively but non-attended?
