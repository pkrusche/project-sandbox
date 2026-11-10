# Concurrent builds and image selection

CLI invocations sharing a generated `.project-sandbox` directory wait for each
other. The context lock is acquired before rendering or synthesizing build
inputs and remains held through image selection. This keeps policy scripts
stable while the builder reads them. Generation-only and `--build-only` invocations
take the same lock. Dry-runs do not acquire locks or inspect images.

After selecting the image, each CLI session releases the context lock and stages
its [private inputs](security.md#private-session-inputs) outside the checkout.
Configs, credentials, prompts, and environment files are independent, so another
session can build and run while the first remains active. Legacy generated API
environment files and provider keys are removed before building.

A second lock covers the runtime's image store from cache inspection through
building and selecting the session image. It is shared across projects, and
uses the runtime rather than tag spelling so aliases such as `demo` and
`demo:latest` cannot bypass it. Builds within one runtime therefore queue;
sessions can run concurrently after image selection, including for one context.

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
recovery remains follow-up work. Unit tests simulate runtime
image stores and force competing invocations to overlap; live runtime acceptance
checks remain listed in TODO.md.
