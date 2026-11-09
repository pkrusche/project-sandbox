# Tasks

## 1. Spike

- [x] 1.1 On Apple `container` 1.7.1, build a `FROM scratch` assets image from a temporary directory, then build a second image using `COPY --from=<assets tag>`. Separately, build with an absolute `-f` outside the cwd context, with a `<Dockerfile>.dockerignore` beside it. Record the results in design.md and `docs/development.md`. Delete the spike images afterwards. Verify that design.md names exactly one Apple mechanism.

## 2. Docker and Podman

- [ ] 2.1 Replace `sandbox_copy_prefix` with `sandbox_copy_from`, and add `--build-context sandbox=<build/>` for whole-project and `--docker-context` builds, plus `--ignorefile` on Podman. Verify with argv tests per runtime and flow, a splicer test with a user stage named `sandbox`, and renderer tests that the default flow is unchanged.
- [ ] 2.2 Remove the interim copy into `<context>/.project-sandbox/` and the "`--docker-context` must contain `.project-sandbox`" check. Verify with CLI tests that a custom context without `.project-sandbox` is accepted, and that a whole-project launch leaves the context byte-identical.

## 3. Apple container

- [ ] 3.1 Implement the content-addressed assets image (design Decision 1), with a `localhost/` reference, pruning and one rebuild-and-retry when the tag is missing. Verify with argv and flow tests, and with an opt-in container test that builds `--python-uv` on Apple `container` with a planted `.project-sandbox/entrypoint.sh` in the project, and checks that the image contains the generated entrypoint.

## 4. Integration

- [ ] 4.1 Add a barrier-controlled test with two sessions of one project building at once with different base images, asserting that each build's argv and inputs are its own. Update `docs/usage.md` (`--docker-context`) and `docs/security.md`, and note in TODO.md that the build-input half of finding #6 is closed. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- When archiving, remove the `.project-sandbox/` exception from the `host-state-storage` requirement "Generated state lives outside the project" in the main spec.
