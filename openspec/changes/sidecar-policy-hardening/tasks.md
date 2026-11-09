# Tasks

## 1. Captures and verification

- [ ] 1.1 Extend `scripts/e2e-copilot-egress.py` to record methods as well as paths, re-run it, and write the observed pairs into `egress_policy.COPILOT_API_REQUESTS`. Verify that the opt-in suite passes with the narrowed profile.
- [ ] 1.2 Add an opt-in sidecar e2e probe for each official GitHub repository transport (HTTPS clone, LFS, release asset download, archive, raw, gist, API, packages). Add any host it reaches that isn't listed to `GITHUB_REPOSITORY_HOSTS`. Verify that every probe is denied.

## 2. Wildcards, PSL and HTTP

- [ ] 2.1 Bundle a dated PSL snapshot with a matcher, reject public-suffix wildcards in `normalize_host_pattern`, and add the refresh step to `scripts/update-pins.py`. Verify with tests for `*.co.uk`, `*.github.io`, `*.amazonaws.com` (rejected), `*.example.co.uk` (accepted) and the `!` exception rules.
- [ ] 2.2 Render exact and wildcard ACLs separately (design Decision 1), stop wildcard-covers-apex deduplication, and warn when a wildcard's apex isn't listed. Verify with renderer tests and with a running-sidecar test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`) in which `*.example.com` denies `example.com` and allows `a.example.com`.
- [ ] 2.3 Add `[egress] http_allow` with its validation (must be allowed; no provider or GitHub hosts), the HTTPS-only rules and the `plain-http-not-allowed` cause and page. Verify with policy tests, renderer tests and running-sidecar tests for denied and allowed plain HTTP.

## 3. Copilot interception

- [ ] 3.1 Render the method, exact-path, path-trick and Host-header ACLs (design Decision 3). Verify with renderer tests.
- [ ] 3.2 Add running-sidecar fixture tests, with the Copilot profile and a stub upstream, for:
  - unrecorded methods and paths;
  - `/copilot_internal/../repos/x`;
  - `%2e%2e`, `%2f` and `%5c`;
  - a conflicting `Host` header;
  - a 3xx redirect to a repository host, which the client's follow-up CONNECT must hit as a denial.

  Verify that every case is denied with `path-not-allowed`, or `host-not-allowed` for the redirect target.

## 4. Providers, revocation, upstream, logs

- [ ] 4.1 Add `[egress] providers`, wire it into `effective()`, and refuse uncovered agents at launch. Verify with policy and CLI tests for `"all"`, a list and `[]`, with and without an agent proxy.
- [ ] 4.2 Render `client_lifetime` from `[egress.sidecar] tunnel_lifetime`, and add `allow remove --terminate` with confirmation and `--yes`. Verify with renderer tests, CLI tests (prompt, `--yes`, non-interactive refusal, the plain `remove` message), and a running-sidecar test that a tunnel is closed after a short configured lifetime.
- [ ] 4.3 Add the upstream parent notice to `status`, the launch path and `doctor`. Verify with output tests.
- [ ] 4.4 Render `strip_query_terms on`, add a log-format guard test, and pass log-rotation options on Docker (json-file or local drivers) and Podman. Verify with renderer tests and argv tests for each runtime and log driver.
- [ ] 4.5 Add running-sidecar tests for direct-mode address handling: mixed public and private answers (denied), an IPv4-mapped IPv6 answer (denied), and a NAT64 prefix answer (documented behavior). Verify that the results match the spec, and record NAT64 behavior in `docs/egress-sidecar.md`.

## 5. Docs

- [ ] 5.1 Update `docs/egress-sidecar.md` and `docs/security.md`: HTTPS-only and `http_allow`, wildcard semantics and PSL rejection, the GitHub host list wording, Copilot pairs, `providers`, tunnel lifetime and `--terminate`, upstream responsibility, log bounds, and the "What splicing can't see" section. Remove the sidecar rows from TODO.md's networking table, and the Squid logging part of "Secrets and logs". Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
