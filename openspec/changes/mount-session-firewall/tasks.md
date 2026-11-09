# Tasks

## 1. Delivery

- [ ] 1.1 Render the firewall into the session's `staging/<id>/firewall/`, mount it read-only at `/project-sandbox-firewall` whenever the firewall is enabled, and remove the firewall `COPY` from `Dockerfile.j2` and the script from the fingerprinted build inputs. Verify with renderer tests (no firewall in any Dockerfile), argv tests per runtime (mount present with the firewall, absent with `--no-firewall`), and a build-cache test that two launches differing only in `--extra-domain` share one fingerprint.
- [ ] 1.2 Point the root stage at the mounted script, add the fail-closed check (design Decision 2), and bump the privilege-model label to `root-stage-v2`. Verify with renderer tests for the enabled, disabled and missing-script branches, and a CLI test that `--no-build` refuses a `root-stage-v1` image.
- [ ] 1.3 Add a CLI test for TODO.md's first item: `--build-only --pi-ollama`, then `--agent pi --pi-ollama --no-build`, where the session's mounted firewall contains the Ollama rule. Verify that it passes, and remove the item from TODO.md.

## 2. Runtime verification and docs

- [ ] 2.1 Add an opt-in container test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`) that starts a session with the firewall mount removed, and asserts a non-zero exit before the session command. Verify on Docker and Apple `container`.
- [ ] 2.2 Extend the privileges e2e check so a normal session shows the mounted script read-only to the agent and the firewall rules installed. Verify on Docker, rootless Podman and Apple `container`.
- [ ] 2.3 Document runtime firewall delivery in `docs/security.md`, and update "Image Builds" in `docs/usage.md` (network options never rebuild). Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
