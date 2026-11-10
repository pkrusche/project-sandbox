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

## Workspace ownership

A `--branch` session holds an exclusive host lease from workspace setup through
build, agent execution, and finalization. Another invocation using the same
branch/bookmark or resolved workspace directory is rejected with an “already in
use” message. Changing `--worktree-dir` or spelling the repository through a
symlink does not bypass ownership. Different branches/bookmarks can run together.
A kept workspace becomes reusable when its owning session finishes. Build
failures and failed sessions release their leases too; dry-runs acquire none.

Git setup and finalization share one repository mutation lock. Jujutsu setup,
finalization, and build-failure removal likewise share one mutation lock. These
short locks protect shared repository changes; the per-session lease also
prevents a delayed finalizer from removing a newer session's workspace.

Git index locks are never automatically removed. Setup of a kept worktree and
finalization query Git for the actual index path. If its lock exists, the
worktree is preserved and the lock path is reported. Stop any Git process,
editor, or container using that worktree, verify the lock is stale, and only then
remove it manually and retry. The host lease cannot prove that an unrelated Git
process has stopped, and manual Git operations do not take these advisory locks.

Leases coordinate live host CLI processes, not external devcontainers or manual
VCS commands. Uncatchable host termination releases advisory leases and may
leave a container running; stop that container before reusing its workspace.
Runtime-aware orphan recovery remains tracked in TODO.md.
