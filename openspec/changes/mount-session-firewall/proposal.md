# Proposal

## Why

The rendered firewall script is `COPY`ed into the session image at
`/usr/local/bin/project-sandbox-init-firewall`, so network policy is part of
image identity:

- **Any policy change means another image.** Changing extra domains, a proxy,
  Ollama, the agent or the egress mode rebuilds the image and moves the
  project's shared tag. On Apple container every rebuild re-exports the whole
  image (about 15 s even when cached); the security e2e suite still rebuilds
  for each `--extra-domain` variant.
- **Stale policy under `--no-build`.** `docs/usage.md` warns that "`--no-build`
  cannot change the firewall baked into that image". A `--no-build` session
  runs whatever policy the image was built with, not the one it asked for.
  The Ollama case of this was fixed (`0b9592d`); every other firewall setting
  can still mismatch.
- **A wrong image is a wrong firewall.** Session pinning prevents a tag move
  from swapping the image mid-launch, but nothing ties the image's baked
  policy to the session's options.

This ports the `sec-fixing-claude` branch's `mount-session-firewall` draft.
That branch ran the script from a root stage (`isolate-firewall-helpers`);
this line keeps its one-shot sudo rule, so a small baked launcher executes the
mounted script.

## What Changes

- **The firewall script is a per-session runtime input,** rendered into the
  session's private inputs directory and mounted read-only at
  `/project-sandbox-firewall/`.
- **Images carry a fixed launcher, not a policy.**
  `/usr/local/bin/project-sandbox-init-firewall` becomes a root-owned launcher
  that runs `/project-sandbox-firewall/init-firewall.sh` with the same
  protections (`cd /`, fixed `PATH`, `python3 -I`, the run-once guard and sudo
  revocation). Changing domains, proxies, Ollama, agent or egress mode never
  rebuilds the image.
- **Fail closed.** Unless the host started the container with
  `--no-firewall`, the entrypoint requires the launcher to succeed, and the
  launcher requires the mounted script; otherwise the container exits before
  the session command.
- **Old images are refused under `--no-build`.** New images carry the label
  `org.project-sandbox.firewall-delivery=mounted`; `--no-build` refuses an
  image without it and says to rebuild.
- What the firewall admits is unchanged (`scope-firewall-allowlist`).

## Capabilities

### New Capabilities
- `session-firewall-delivery`: how a session's firewall policy reaches the
  sandbox (a per-session, read-only runtime input run through the baked
  launcher), what happens when it is missing, and how older images are refused.

### Modified Capabilities

None.

## Impact

- Code: `firewall.py`/`cli.py` (render into the session inputs directory, the
  mount), `container_cli.py` (read-only mount when the firewall is enabled,
  label check under `--no-build`), `templates/Dockerfile.j2` (launcher instead
  of the script, the label), a new launcher template, `build_cache.py` (the
  firewall script leaves the fingerprinted inputs).
- Depends on `minimal-devcontainer` landing first: today's devcontainer also
  runs the baked firewall from `postStartCommand`; after that change it has
  none.
- Interplay: `validate-all-mount-sources` forbids user targets under
  `/project-sandbox-*`; `pin-launch-image-identity` needs no network fields in
  its labels; the security e2e suite's shared images then also cover the
  `--extra-domain` test.
- Images rebuild once.
- Docs: `docs/security.md` (runtime firewall delivery), `docs/usage.md`
  ("Image builds", the `--no-build` note).
