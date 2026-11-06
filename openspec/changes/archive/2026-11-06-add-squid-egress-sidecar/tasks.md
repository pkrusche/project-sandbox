# Tasks

## 0. Spikes (block groups 3+)

- [x] 0.1 On Apple `container` ≥1.4.1, check the following, and record the commands and results in design.md Decision 1:
  - Create a `--internal` network.
  - Run a container with two `--network` flags (internal and default).
  - Confirm that a container on the internal network alone cannot reach a public address but can reach the dual-homed container.
  - Confirm which host addresses are reachable from the internal network.
  - Check whether `host.docker.internal` still resolves from the sidecar.

  If two networks per container are unsupported, prototype the fallback topology and pick one. Verify by documenting the chosen topology and the commands it uses.
- [x] 0.2 Build a candidate Squid 6.x Alpine image with `security_file_certgen`. With `cache_peer … parent` and `never_direct allow all`, confirm that both spliced and bumped CONNECTs are tunnelled through the parent, by capturing the parent's access log. Also confirm that `ssl::server_name` vs CONNECT host matching and `on_unsupported_protocol` close the upstream `connect-sni-mismatch` and `connect-raw-tunnel` probes. Verify by recording the image version and results in design.md Decisions 2 and 7.
- [x] 0.3 On Docker Desktop and Podman, confirm that `network create --internal` combined with `network connect` gives the sandbox no Internet route, and record which host or gateway addresses remain reachable. Verify by recording the results in design.md Decision 1.

## 1. Confirm external facts

- [x] 1.1 Port the relevant probes from internet-proxy-locally (`lab/`, `scripts/`) and record their source commit in design.md. Verify that the probe list matches the e2e requirement.
- [x] 1.2 ~~Enumerate GitHub URL shapes for repository rules.~~ Deferred: per-repository GitHub access moved to ROADMAP.md ("Smart GitHub repository access"). Sidecar mode blocks GitHub repository access instead (Decision 4).
- [x] 1.3 Confirm the Copilot CLI host and path set, and whether it needs `api.github.com` (`COPILOT_API_ACCESS` `deny` vs `paths`). `scripts/e2e-copilot-egress.py` (suite `copilot-egress`, billable) captures a real session and verifies the profile. Verify by running `scripts/run-e2e-tests.sh --only copilot-egress`, then recording the report in design.md Decision 5 and updating `COPILOT_HOSTS`, `COPILOT_API_ACCESS` and `COPILOT_API_PATHS`.

## 2. Policy model and validation

- [x] 2.1 Add `egress_policy.py` with these parts. Verify with `tests/test_egress_policy.py`.
  - A TOML loader (`tomllib`) for `[egress]` and `[egress.copilot]`. `[egress.github]` is rejected with a roadmap pointer.
  - Strict hostname/wildcard validation, with unknown-key rejection, and rejection of GitHub repository hosts (including covering wildcards).
  - Merging of `--extra-domain`, the Copilot profile and agent provider hosts.
  - Bump-set derivation (`api.github.com` only, in Copilot `paths` mode; otherwise empty).
- [x] 2.2 Resolve the default `$XDG_CONFIG_HOME/project-sandbox/egress.toml`. Reject policy paths inside the project, worktree, main `.git`, or any sandbox mount, after `resolve()`, including through symlinks. Verify with test cases for each rejected location and a symlink into the project.

## 3. CLI surface

- [x] 3.1 Add `--egress {firewall,sidecar}` (default `firewall`) and `--egress-policy PATH`. Reject sidecar with `--no-firewall` or chroot, and `--egress-policy` without sidecar, before any work. Sidecar launches skip devcontainer rendering. Until group 6 lands, a non-dry-run sidecar launch is rejected. Verify with `test_cli.py` acceptance and rejection cases, and a regression test that default-mode run argv and rendered files are byte-identical to before.
- [x] 3.2 Relax the `--internet-proxy` conflicts with `--extra-domain` and `--allow-github` in sidecar mode only, and update the firewall-mode error text to mention `--egress sidecar`. Verify with `test_cli.py` cases for both modes.
- [x] 3.3 Implement the `--dry-run` preview: policy path, effective allowlist, Copilot profile, bump-set, CA constraints, network and sidecar argv. Verify with a `test_cli.py` test asserting the output, and that no files, keys, networks or containers are created (mocked subprocess not called for `network create` or `run`).

## 4. Session CA

- [x] 4.1 Add `session_ca.py` to generate an EC P-256 CA with `BasicConstraints(ca, pathlen=0)`, `KeyUsage(keyCertSign, cRLSign)`, critical `NameConstraints` permitting the bump-set hosts' registrable domain (`github.com`, because Squid mimics the origin certificate's names) (or `invalid` when the bump-set is empty, because Squid's `ssl-bump` port always needs a certificate), and a bounded validity. Write the files into the session directory with mode 0700 for the directory and 0600 for the key. Verify with `tests/test_session_ca.py`, which parses the certificate and asserts the extensions and file modes.
- [x] 4.2 Delete the CA directory at teardown, and remove stale ones on launch. Verify with tests for normal exit, an exception path, and a stale directory.

## 5. Squid sidecar image and config

- [x] 5.1 Add the sidecar Dockerfile and entrypoint templates: pinned Squid 6.x (candidate `alpine:3.22.1` + `squid-6.12-r0`), `security_file_certgen`, the `sni-match` external ACL helper, `socat` with an unprivileged local-service forwarder per enabled port (`range=` the internal subnet), CA staged to tmpfs, and Squid started as `squid` via `su-exec` with stdout piped through a `squid`-owned process. Register the pin in `scripts/update-pins.py` and cache the image by checksum. Verify with `test_renderers.py` and `test_update_pins.py`.
- [x] 5.2 Add `squid.conf.j2` implementing the Decision 2 pipeline. Verify with `test_renderers.py` assertions for each part, including that every CONNECT-level deny precedes the bump rules and uses `deny_info TCP_RESET` with a `note sbx_cause` annotation, that the SNI check uses the `sni-match` external ACL, and that no `on_unsupported_protocol tunnel` is present.
  - Allowlist ACLs.
  - Port, IP-literal and SSRF denials (SSRF in direct mode only).
  - peek/SNI-match/bump/splice steps.
  - `on_unsupported_protocol` deny.
  - Labelled `deny_info` pages.
  - Logging to stdout/stderr.
  - No caching.
- [x] 5.3 Render the Copilot profile from Decision 5 (spliced Copilot hosts; in `paths` mode, `api.github.com` bumped with `COPILOT_API_PATHS` allowed and every other path denied with a labelled page). Verify with `test_renderers.py` cases that evaluate the rendered `urlpath_regex` in Python against `/copilot_internal/v2/token`, `/user`, `/repos/o/r`, `/repos/o/r/contents/x`, `/gists`, `/graphql` and `/user/repos`, and that no other GitHub repository host appears in any ACL.
- [x] 5.4 Render the parent-proxy mode: `cache_peer` with TLS and `tls-cafile` from `--ca-cert`, `never_direct allow all`, no `dst` ACLs, and `tls_outgoing_options cafile=` including `--ca-cert`. Verify with `test_renderers.py` cases for an HTTP parent, an HTTPS parent, and the Apple relay parent.

## 6. Runtime orchestration

- [x] 6.1 Add `egress_sidecar.py` to build the argv for each runtime (Docker, Podman, Apple) to create the labelled internal network, start the sidecar on both networks (Podman: internal network created with `--disable-dns`), read the sidecar's internal IP by `inspect`, and start the sandbox on the internal network only, with `SBX_EGRESS_SIDECAR_IP` set. Pass the enabled local-service ports and the internal subnet to the sidecar forwarder, reject local-service ports below 1024, and do not forward a local `--internet-proxy` to the sandbox. Verify with `tests/test_egress_sidecar.py` argv assertions for each runtime, with no runtime required.
- [x] 6.2 Implement the readiness wait (denied-sentinel CONNECT, bounded timeout), log-tail reporting on failure, and `finally` teardown of the sidecar, network and CA. Verify with tests that use a mocked runner for success, timeout, and sandbox-start failure, each asserting the cleanup calls.
- [x] 6.3 Clean up orphans by label for sessions without a held lock (`host_locks`), both at launch and in `doctor`. Verify with tests that use mocked `network ls` / `ps` output.
- [x] 6.4 On Apple, allocate a per-session relay port for the parent-proxy relay in sidecar mode only, and leave firewall-mode relay port derivation unchanged. Verify with `test_internet_proxy.py` cases for both modes, and a test that two concurrent sidecar sessions get distinct ports.

## 7. Sandbox side

- [x] 7.1 Add the `sidecar` firewall policy to `firewall.py` and `init-firewall.sh.j2`: default DROP, the sidecar IP on 3128 and on each enabled local-service port, no host destinations, no DNS, no ipset domain pinning. The entrypoint's root phase writes `/etc/hosts` entries mapping `egress-proxy` and `host.docker.internal` to `SBX_EGRESS_SIDECAR_IP`. Verify with `test_renderers.py` assertions that no `dig`/ipset domain loop, provider list or host-address rule is rendered, and that the sidecar rules and hosts entries exist.
- [x] 7.2 Add a runtime CA install to the root phase of `entrypoint.sh.j2`: when `/project-sandbox-egress/ca.pem` exists, install it and run `update-ca-certificates` before dropping privileges, then export `NODE_USE_SYSTEM_CA=1`, `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`. Mount the public certificate read-only only in sidecar mode with interception. Verify with `test_renderers.py` and `test_container_cli.py` mount presence and absence cases, including that no mount ever references `ca-key.pem`.
- [x] 7.3 Point the proxy environment at `http://egress-proxy:3128` in sidecar mode, keeping the local-service `NO_PROXY` entries (`host.docker.internal` now resolves to the sidecar forwarder). Verify with `test_internet_proxy.py` environment cases.

## 8. Observability

- [x] 8.1 Persist the sidecar access log to the session state directory. At session end, print a bounded denied-destination summary with cause, attribution (local or upstream) and the policy path. Verify with a `test_observability.py` case that parses sample Squid log lines covering each labelled cause (including `TCP_RESET` rows logged as `TCP_DENIED/200` with a `sbx_cause` note, `terminate` rows, and `peek ERR_PROTOCOL_UNKNOWN` rows) and an upstream 403.

## 9. Verification

- [x] 9.1 Add the opt-in `scripts/e2e-egress-sidecar.py`, registered in `scripts/run-e2e-tests.sh` as `egress-sidecar`, with every check listed in the e2e requirement, including parent chaining through an intercepting fixture proxy with its own CA (reusing the fixtures from `fixture_network.py`), local-service forwarding through the sidecar on each runtime (including the Podman pasta strategy), and that a default-network container cannot use the forwarder. Verify that `uv run pytest -q` covers its argument parsing without a runtime, and run the suite on Docker and Apple `container`.
- [x] 9.2 Run `uv run python -m compileall src tests` and `uv run pytest -q`. Verify both pass.

## 10. Documentation

- [x] 10.1 Write `docs/egress-sidecar.md`. Verify that the documented commands match the CLI help and that the dry-run examples run as written. It covers:
  - Mode selection, topology and policy file format.
  - GitHub repository blocking (what fails, and firewall mode with `--allow-github` as the alternative).
  - The Copilot profile.
  - Parent proxy setup, including the intranet-reachability warning and Apple relay notes.
  - Per-client trust remedies (Java, certifi in virtualenvs, Chromium NSS).
  - Troubleshooting denials.
- [x] 10.2 Update `docs/security.md` (CA key handling, name constraints, the policy-location rationale), `docs/internet-proxy.md` and `docs/proxy-endpoints.md` (sidecar parent mode), `ROADMAP.md` (the Apple relay concurrency item is resolved for sidecar mode), `CHANGELOG.md`, and add a one-line `README.md` mention. Verify that the links resolve.
- [x] 10.3 Add deferred items to `TODO.md`. Verify the entries exist. ("Smart GitHub repository access", including credential injection and GraphQL handling, is already in `ROADMAP.md`.)
  - Upstream proxy authentication.
  - Sidecar mode for devcontainers.
  - Making sidecar mode the default.
