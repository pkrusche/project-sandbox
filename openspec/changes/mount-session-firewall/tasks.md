# Tasks

## 1. Delivery

- [ ] 1.1 Render the firewall into `<session inputs>/firewall/`, mount it read-only at `/project-sandbox-firewall` whenever the firewall is enabled, replace the baked script with the launcher and the label in `Dockerfile.j2`, and drop the script from the fingerprinted build inputs (design Decisions 1 and 3). Verify with renderer tests (no policy in any Dockerfile, the launcher and label present), argv tests per runtime (mount present with the firewall, absent with `--no-firewall`), and a build-cache test that two launches differing only in `--extra-domain` share one fingerprint.
- [ ] 1.2 Add the fail-closed entrypoint and launcher checks (Decision 2). Verify with renderer tests for the enabled, disabled and missing-script branches.
- [ ] 1.3 Refuse `--no-build` images without the label. Verify with a CLI test against a stubbed image inspect, and a dry-run test that inspects nothing.

## 2. Runtime verification and docs

- [ ] 2.1 Add an opt-in container test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`) that starts a session with the firewall mount removed and asserts a non-zero exit before the session command. Verify on Docker and Apple `container`.
- [ ] 2.2 Extend the firewall e2e suite: the mounted script is read-only to the agent, the rules are installed, and a second launch differing only in `--extra-domain` reuses the image. Verify on Docker, rootless Podman and Apple `container`; then drop `--extra-domain` from the security suite's `IMAGE_OPTIONS`.
- [ ] 2.3 Document runtime firewall delivery in `docs/security.md`, and update "Image builds" and the `--no-build` note in `docs/usage.md`. Verify that `uv run pytest -q`, `uv run ruff check src scripts tests` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- Land after `minimal-devcontainer`.
- Archive with `/opsx:archive`, creating `openspec/specs/session-firewall-delivery/`.
