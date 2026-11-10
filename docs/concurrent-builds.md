# Concurrent builds and image selection

CLI invocations sharing a generated `.project-sandbox` directory wait for each
other. The context lock is acquired before rendering or synthesizing build
inputs and remains held through session teardown. This keeps policy scripts
stable while the builder reads them and keeps post-build provider keys out of
another invocation's build. Generation-only and `--build-only` invocations
take the same lock. Dry-runs do not acquire locks or inspect images.

The lock lasts for the session because provider configuration, prompts, and
some environment files still use shared paths. Stale generated API environment
files are removed before a build and on session exit; stale provider configs
are cleared by rendering. Private per-session staging is planned in TODO item
8 to restore concurrent sessions using the same context. Branch flows that
build inside separate workspaces can use separate contexts.

A second lock covers the runtime's image store from cache inspection through
building and selecting the session image. It is shared across projects, and
uses the runtime rather than tag spelling so aliases such as `demo` and
`demo:latest` cannot bypass it. Builds within one runtime therefore queue;
sessions using separate contexts can run concurrently after image selection.

Docker and Podman launch the full local image ID. Apple container receives a
unique `localhost/project-sandbox-session:<random>` alias, whose content digest
is checked before launch. The alias stays until teardown, then is deleted.
This avoids Apple's reported [local-image digest lookup limitation](https://github.com/apple/container/issues/1962).
The `project-sandbox-session` repository name is reserved. Inspection or
pinning failures abort launch. `--no-build` pins the existing image too; it
does not check that the image implements newly requested policy flags.

Cache reuse requires matching generated inputs, runtime, build identity, tag,
and current image identity. A changed tag or legacy cache record without an
image identity causes a rebuild. Fingerprints include generated certificate
contents and Docker ignore files. Whole-project build contexts continue to
build each time using the runtime's layer cache.

These advisory locks coordinate project-sandbox CLI processes. External
devcontainer builders and manual runtime operations do not take them. Avoid
regenerating a context used by an externally managed devcontainer. Uncatchable
termination can leave running containers and Apple aliases behind; lifecycle
recovery and private staging remain follow-up work. Unit tests simulate runtime
image stores and force competing invocations to overlap; live runtime acceptance
checks remain listed in TODO.md.
