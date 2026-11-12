# Tasks

## 1. IDE verification

- [x] 1.1 Hand-render a `devcontainer.json` with `--network none` for a `--python-uv` project and open it in VS Code. Done on the `sec-fixing-claude` branch (2026-11-09); findings are in the design's Risks.

## 2. Image and firewall

- [ ] 2.1 Add a devcontainer profile to `dockerfile.render` and `Dockerfile.j2`: no agent installs, no firewall `COPY`, no sudoers rule, no runtime CA trust; build-time CA handling kept. Verify with renderer tests that `Dockerfile.devcontainer` contains no `npm install -g` for agent packages, no `sudoers` and no `init-firewall`, and that `Dockerfile` (the session image) is unchanged.
- [ ] 2.2 Remove the `init-firewall-devcontainer.sh` render and the `allow_host_network` branch from `firewall.py` and `init-firewall.sh.j2`. Verify with renderer tests that no rendered firewall variant contains the host-gateway block, and that `.gitignore` handling and `build_cache._BUILD_INPUT_FILES` no longer list the devcontainer firewall file.

## 3. Provisioning

- [ ] 3.1 Split `_provision.sh.j2` into identity and agent parts. Include both in `entrypoint.sh.j2` and only identity in `devcontainer-entrypoint.sh.j2`. Verify with renderer tests on both entrypoints, and that the session entrypoint output is byte-identical to before the split.

## 4. devcontainer.json

- [ ] 4.1 Rewrite `devcontainer.render` per design Decision 5:
  - add `--network none` and the offline `containerEnv`;
  - remove capabilities, credential and config mounts, Claude history, agent env, agent extensions and the firewall `postStartCommand`;
  - keep identity, shell history, `--mount` values, masks and limits;
  - remove stale `claude*`/`codex*` symlinks in `.devcontainer/`.

  Verify with `tests/test_devcontainer.py` cases asserting every scenario in `devcontainer-environment`.
- [ ] 4.2 Remove the `claude-devcontainer`/`codex-devcontainer` profiles from `config_agents.render` and the devcontainer staging from `sync_credentials`. Verify with `tests/test_cli.py` that a forwarding launch creates no devcontainer staging directory.
- [ ] 4.3 Keep the `--vcs-access` mounts, the private-storage history and the `initializeCommand` helper in the reduced `devcontainer.json`. Verify with the existing devcontainer reopen test (`test_private_mounts_and_devcontainer_reopen`) and renderer tests for `read-only` and `hidden`.

## 5. CLI

- [ ] 5.0 Create and always mask `.devcontainer/` on every launch (design Decision 7). Verify with argv tests for firewall and sidecar launches with and without an existing directory, and a symlink refusal test.
- [ ] 5.1 Add `--devcontainer` as a terminal mode (design Decision 6): render and exit, with its rejected combinations, and remove the render from the launch path together with `_renders_devcontainer`. Verify with CLI tests that an ordinary launch in firewall and sidecar mode writes no devcontainer file, that `--devcontainer` writes them and starts nothing, each rejected combination, and `--devcontainer --dry-run`.
- [ ] 5.2 Add `--devcontainer-network {none,runtime}` (default `none`), valid only with `--devcontainer`. In `runtime` mode: no `--network` override, the `--internet-proxy` environment when configured, no offline variables, and an unfiltered-network warning. Verify with CLI tests for both modes, the invalid value, and the flag without `--devcontainer`.

## 6. Verification and docs

- [ ] 6.1 Add an opt-in container test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`) that builds `Dockerfile.devcontainer` for a `--python-uv` fixture and runs it with the rendered `runArgs`. It checks that:
  - `uv run pytest` passes offline;
  - a TCP connection to a public address fails;
  - a loopback server works;
  - `sudo -n true` fails;
  - `/workspace/.devcontainer` is read-only.

  Verify on Docker and Podman.
- [ ] 6.2 Rewrite `docs/usage.md` "Devcontainers" (purpose, `--devcontainer`, no agents, offline default, `--devcontainer-network`, IDE findings from task 1.1). Update `docs/security.md` (remove the devcontainer credential, gateway and firewall-variant text, and describe the new profile) and the `README.md` feature line. Remove this change's row from TODO.md's "Security proposals" table. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
