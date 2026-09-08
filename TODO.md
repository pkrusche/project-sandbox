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
  - The injected cert enters the build via a new `--ca-cert PATH` CLI option
    (repeatable, like `--extra-domain`) that `Dockerfile.j2` renders a
    `COPY`+`update-ca-certificates` step for. Since the cert is only used in 
    the session and not during the build, this step should be last.
  - Let's run `update-ca-certificates` (OS trust store), no need to set
    `NODE_EXTRA_CA_CERTS`/`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` in the
    container environment, but since we use node, let's configure node to use
    the correct system certificates, including the injected one.
  - We will assume the cert is for an arbitrary corporate TLS-inspecting proxy 
    sitting in front of *all* egress, but to avoid interactions with IP address
    pinning, we would enforce --ca-cert to always be used with --internet-proxy
    s.t. we know traffic will go through the proxy.
  - The certificate should be a build time input, it gets baked into the Docker
    image.
  - This should apply uniformly to `--dockerfile`, `--base-image`, and the
    generated `.devcontainer/`.
