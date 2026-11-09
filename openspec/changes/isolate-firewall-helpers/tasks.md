# Tasks

## 1. Self-protecting firewall script

- [ ] 1.1 In `init-firewall.sh.j2`, `cd /` before any other command, run both embedded helpers as `python3 -I -`, and make every `curl` call `curl -q` (first argument). Verify with a renderer test asserting that, in every policy variant (normal, internet-proxy, sidecar, devcontainer), every `python3` invocation carries `-I` and every `curl` invocation starts with `-q`.
- [ ] 1.2 Add a regression test that renders the firewall with local and remote destinations, extracts the `PY_ENDPOINTS` and `PY_PREFLIGHT` heredocs, and runs each with the rendered interpreter flags from a temporary directory containing marker `ipaddress.py`, `socket.py`, `shlex.py`, `ssl.py` and `pathlib.py`. Verify no marker fires. Add a control run without `-I` that does fire it, so the test proves the setup is effective.
- [ ] 1.3 Add a renderer test asserting the rendered script references no caller environment variables (no `$PROJECT_SANDBOX_*`, `$HOME`, `$PWD`) and contains no relative paths in root-run commands. Verify it fails on a deliberately injected `$PWD` reference.

## 2. Root stage and privilege drop

- [ ] 2.1 Add a root-stage template (`project-sandbox-init`) that does `cd /`, runs the firewall under `env -i PATH=<fixed> HOME=/root` unless the firewall is disabled, and execs `setpriv --reuid/--regid/--init-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs` into the existing entrypoint. Verify with renderer tests on the exact `setpriv` argv and on the firewall-disabled branch.
- [ ] 2.2 Remove both `sudo -n …init-firewall` calls from `entrypoint.sh.j2`. Keep the quiet-mode failure capture in the root stage, and have the entrypoint `cd /workspace` after the drop. Verify with renderer tests that the entrypoint contains no `sudo`, and that root-stage firewall failure exits non-zero before the exec.
- [ ] 2.3 In `Dockerfile.j2`: install and verify `setpriv` (`command -v setpriv` fails the build), copy the root stage, set `ENTRYPOINT` to it, keep `USER agent`, add `LABEL org.project-sandbox.privilege-model="root-stage-v1"`, and render the sudoers rule only for the devcontainer variant, with explicit `env_reset`/`always_set_home`/`secure_path` defaults. Verify with renderer tests for the agent and devcontainer Dockerfiles.
- [ ] 2.4 In `container_cli.run_argv`, add `--user 0:0` for agent sessions on Docker, Podman and Apple `container`. Keep `--userns=keep-id` on rootless Podman. Verify with argv tests per runtime, and with a test that chroot argv is unchanged.

## 3. Refusing pre-change images

- [ ] 3.1 Add an image-label check next to `container_cli.image_exists`, reading `org.project-sandbox.privilege-model` from `image inspect` on each runtime. Verify with tests on captured inspect JSON for Docker, Podman and Apple `container`.
- [ ] 3.2 Refuse `--no-build` launches whose image lacks the label, with a rebuild instruction, before any container starts. Verify with a CLI test that also checks `--dry-run` only performs the read-only inspect.
- [ ] 3.3 Confirm the cache-hit path rebuilds images that predate the label (the fingerprint changes with the Dockerfile). Verify with a `test_build_cache.py` case using a stored fingerprint from the pre-change template.

## 4. Runtime verification and docs

- [ ] 4.1 Add an e2e check (a new `scripts/e2e-privileges.sh`, or a mode of `scripts/e2e-test.sh`) that inside a session asserts:
  - `sudo -n true` and `sudo -n /usr/local/bin/project-sandbox-init-firewall` fail;
  - `/proc/self/status` shows `NoNewPrivs: 1` and `CapBnd: 0000000000000000`;
  - `iptables -F` fails;
  - a `/workspace/ipaddress.py` marker planted before launch never fires, with a local proxy destination configured so the endpoint helper runs;
  - in a devcontainer, a `/home/agent/.curlrc` with an `output` directive, planted before the post-start command, is not honored when the firewall runs through `sudo`.

  Run it on Docker, rootless Podman and Apple `container`. Verify by recording results in `docs/development.md`.
- [ ] 4.2 Verify that a host `exec` into a running agent container lands as `agent`, and that `scripts/verify-timeout-teardown.sh` still passes with the entrypoint as PID 1 after the drop.
- [ ] 4.3 Update `docs/security.md`:
  - describe the root stage, the privilege drop and the devcontainer's interim `sudo` rule;
  - make "the agent runs without root, so it cannot change those rules" accurate;
  - note that the firewall can no longer be re-initialized mid-session.

  Verify the doc against the rendered templates.
- [ ] 4.4 Remove finding #1 from TODO.md's security review, and point to this change from the networking table's "Startup, reinitialization, and revocation" row. Verify `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
