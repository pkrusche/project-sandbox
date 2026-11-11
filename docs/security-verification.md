# Security verification

The cases in `scripts/security_e2e/` exercise real CLI subprocesses, containers,
Git processes, kernel locks, and Squid. They cover the verification bullets and
hardening probes in [TODO.md](../TODO.md). Tests for proposed protections record
known limitations separately; they do not implement those protections.

## Running

Run from the checkout with `uv sync` completed. Select one runtime explicitly:

```bash
uv run pytest -ra -q scripts/security_e2e --security-runtime docker
uv run pytest -ra -q scripts/security_e2e --security-runtime podman
uv run pytest -ra -q scripts/security_e2e --security-runtime apple-container
```

Use `-k` to select a topic, such as `cargo`, `workspace`, `copilot`, `dns`,
`revocation`, or `ollama`. The ordinary `uv run pytest -q` collects `tests/` only;
it does not build images, start these fixtures, or contact providers.

For a release gate, fail on missing prerequisites and known limitations too:

```bash
uv run pytest -ra -q scripts/security_e2e --security-runtime docker \
  --peer-runtime podman --strict-coverage --junitxml=security-docker.xml

# Same gate through the e2e runners:
scripts/run_all_supported_e2e_tests.sh --strict-security-coverage

# Select just security on one runtime:
scripts/run-e2e-tests.sh --only security --runtime docker \
  --security-peer-runtime podman --keep
scripts/run-e2e-tests.sh --only security --runtime apple-container --plan
```

`scripts/run_all_supported_e2e_tests.sh` includes the security suite by default on each
selected container runtime when builds are enabled. It automatically chooses
another selected container runtime as the policy-edit peer; chroot and runtimes
excluded by `--runtimes` are never chosen. With only one container runtime,
cross-runtime coverage remains incomplete unless you explicitly supply a peer.
Use `-- --security-peer-runtime NAME` to override the automatic choice (the
same override applies to every selected runtime), or forward fixture settings
such as `-- --security-fixture-host IP --security-nat64-origin HOST`.

The single-runtime `run-e2e-tests.sh` enables the suite with `--with-security`,
`--only security`, or `--all-tests`. The all-runtime runner skips it with
`--no-build` and on chroot; explicitly requesting security with `--no-build`
is an error. In these runners, recorded known limitations (XFAIL) and missing
prerequisites (SKIP) do not fail the suite, so a green e2e run means no
*unexpected* security failure. Those entries are **not verified protections**:
read the `-ra` summary in the suite log (kept with `--keep`) or a JUnit report,
not only the exit code. Pass `--strict-security-coverage` to either runner for
the release gate, which fails on every XFAIL and SKIP and is expected to fail
while the recorded hardening items remain open.

Logs, fixtures, origin requests, DNS results, dependency inventories, and
inspection evidence are retained under `.project-sandbox/e2e/security.*`.
`--security-artifacts PATH` selects an empty output directory. Use a host path
visible to the runtime VM; Apple's builder cannot use macOS's per-user temporary
directory. Host-only checks default to temporary storage and can run here:

```bash
uv run pytest -ra -q scripts/security_e2e/test_host.py --security-runtime host
```

Tests stop their own sessions and sidecars. A sidecar test refuses to replace an
existing global `project-sandbox-egress` container; stop it before running. Run
these tests sequentially, without pytest-xdist: the global sidecar and runtime
image lock are deliberately shared. No real agent tokens or billable inference
are needed. The Ollama test uses a synthetic Pi executable and loopback HTTP
server while exercising the actual CLI's `--pi-ollama` lifecycle. The other
sessions use Bash and disable credential forwarding and refresh.

## Coverage map

Names below are pytest function names; parametrization covers the listed cases.

| TODO topic | Executable case and evidence |
| --- | --- |
| Cargo literal COPY paths | `test_cargo_literal_paths`: build a Cargo workspace with spaces, both quotes, dollars, wildcards, brackets, semicolons and shell-substitution-shaped directory names; fetch during image build and check offline inside the container. |
| Firewall one-time setup, revocation, import shadowing | `test_existing_mount_and_firewall_audits[firewall]`: two fresh containers, root-owned one-time guard, removed sudo grant, denied sudo replay, hostile workspace Python modules that leave a marker if imported. |
| Concurrent cross-runtime policy edits | `test_concurrent_cross_runtime_edits_and_delayed_launch`: two live runtimes edit one private policy through real CLI commands; both edits persist and removal is observed by a later sandbox. Requires `--peer-runtime`. |
| Launch delayed across revocation | `test_launch_waiting_for_image_lock_cannot_restore_revoked_policy`: hold the real image lock, wait for generation by the delayed CLI, revoke access, release the lock, and require denial through live Squid. |
| Concurrent builds and image identity | `test_concurrent_builds_same_tag_use_their_own_image`: two projects build different canaries under the same tag; each agent must see its own image. `test_concurrent_sessions_private_inputs_and_image_identity` inspects actual container image references. |
| Private inputs and concurrent provisioning | The concurrent-session case requires two private input trees, checks a prompt remains unchanged throughout each session, exits one while the other remains active, and checks both normal cleanups. |
| Apple alias retention/deletion | The concurrent-session case checks distinct live aliases resolve to the selected digest and disappear after teardown. |
| Workspace lifetime ownership and host capture | `test_workspace_lease_and_capture`: Git/jj × full/read-only/hidden, overlapping same-ref launch rejected while owner remains live, subsequent reuse succeeds, and a source edit is captured into the host branch/bookmark. |
| Live external Git lock | `test_live_external_git_lock_is_preserved`: a separate real `git update-index --index-info` process holds the worktree's actual index lock while CLI reuse is refused; its inode/content remain intact. |
| VCS metadata writes, unlink/rename and hiding | `test_existing_mount_and_firewall_audits[vcs-access]`: real Git/jj metadata mounts, direct write/unlink/rename attempts, full metadata hashes before/after, persisted full-access commit, read-only history, and hidden contents. |
| Private mount storage and devcontainer reopen | `test_private_mounts_and_devcontainer_reopen`: run the generated standalone `initializeCommand`, apply generated mounts on the selected runtime, delete empty private workspace masks, and repeat initialization/launch, including comma/quote-bearing project names. Malformed mount rejection is recorded as an encoding limitation. This tests the reopen helper and mounts, not VS Code's UI. |
| Sole Ollama owner SIGKILL | `test_only_pi_ollama_owner_sigkill_and_reattach`: build with `--build-only --pi-ollama`, prove container reachability, kill the sole host CLI, require bridge listener release within one poll interval plus scheduling allowance, stop its orphan container, and verify a fresh owner/forwarder works. |
| Private DNS pins and unexpected ports | `test_private_domain_pin_opens_unexpected_tcp_port`: controlled host listener works without a firewall, is denied without a pin, and is probed with a private-address DNS pin. Reports current unrestricted-port behavior as a known limitation. |
| Devcontainer gateway DNS/host ports | `test_devcontainer_gateway_unexpected_port_and_dns`: generated devcontainer firewall image, controlled gateway TCP listener and UDP/53 responder. Reports broad gateway access as a known limitation. |
| Spliced TLS domain fronting and plain HTTP | `test_spliced_domain_fronting_and_http_default`: a controlled origin accepts differing CONNECT/SNI and encrypted Host; compare allowed control, denied CONNECT, fronted request and HTTP request. Establishes the splice limit without asserting any real provider supports fronting. |
| Wildcard apex/public suffix and implicit providers | `test_wildcard_apex_public_suffix_and_implicit_providers`: apex/subdomain requests, implicit provider request, and real `allow add '*.co.uk'`. |
| GitHub repository transports | `test_repository_host_exclusions`: each configured repository host plus `lfs.github.com`, via HTTPS, plain HTTP, SSH-port CONNECT and Git-port CONNECT. These are host/port boundary probes, not authenticated clone or LFS transfer tests. |
| Copilot path normalization | `test_copilot_paths`: raw dot segments, encoded dots/slashes/prefix, explicit repository path, and query-bearing allowed path, with an allowed-path positive control. Ambiguous repository paths reaching the origin fail. |
| Conflicting Host/CONNECT/SNI, methods, redirects | `test_copilot_host_sni_methods_and_redirect`: differing SNI and Host, arbitrary DELETE method, controlled redirect and request to its forbidden destination. Method breadth remains a known limitation. |
| Direct private destinations, mapped IPv6/NAT64 | `test_direct_private_and_translated_literals` requires logged ACL denials. `test_mixed_dns_rebinding_mapped_ipv6_and_nat64` installs a temporary local authoritative DNS fixture in the disposable sidecar, returns mixed public/private answers, changes a previously admitted address to loopback, and tests mapped IPv6 and the standard NAT64 prefix through Squid's real resolver/cache. It requires each denial to be logged as `private-address` with no upstream peer, then expected-fails on the documented `200`-before-reset for uncached lookups (Squid sends its `TCP_RESET` before the queued `200` only when `http_access` finishes synchronously). |
| Parent-proxy trust | Controlled-origin tests deliberately terminate all forwarded traffic at a loopback parent. Direct-mode private classification is tested separately; an upstream parent must supply its own destination restrictions. The existing `egress-sidecar --checks parent` suite additionally tests parent isolation/forwarding. |
| Deployment-specific NAT64 | `test_deployment_nat64_path` requires an actual deployment translator and controlled TLS origin (`--nat64-origin`, aggregate `--security-nat64-origin`); it proves direct reachability first, then checks the real Squid path and logged denial. See below. |
| Startup, established connections, ICMP capabilities | `test_startup_connection_and_agent_icmp_capabilities`: start a controlled connection before initialization, execute the generated initializer, capture kernel rules and unprivileged raw-ICMP socket results, then test the established connection. The fixture controls startup as root; this is not evidence an agent runs before the real entrypoint initializes. |
| Existing tunnels after revocation | `test_revocation_existing_tunnel_vs_new_connection`: hold an actual TLS connection open through the controlled origin, remove policy, compare its survival with denial of a new connection. |
| Mount encoding | `test_mount_roundtrip` checks literal host/container file identity for unusual paths; `test_unsupported_mount_grammar_fails_closed` checks control-character rejection. |
| Host locks | `test_live_lock_contention_and_symlink` uses real processes/flock and a symlink sentinel. FIFO/private-directory and bounded-wait probes report the remaining hardening gaps. |
| Sensitive logs | Query-canary variant of `test_copilot_paths` searches actual Squid logs; a visible canary reports missing query redaction. Only synthetic values are used. |
| Supply chain | `test_installed_tool_dependency_inventory` retains two installed npm dependency inventories and compares them. It explicitly records the missing transitive lock as a limitation: matching cached builds do not establish reproducibility. |

## Runtime and topology prerequisites

- Install Git and, for jj workspace cases, host jj. The runtime needs image-build
  network access. No runtime binary means an explicitly selected runtime fails;
  `--security-runtime host` intentionally skips container cases.
- Apple Git pointer-file restricted mounts are an unimplemented feature and
  those cases skip. jj and plain-directory metadata cases remain available.
- Cross-runtime tests need a second operational runtime whose published sidecar
  endpoint can coexist with the first. Both global sidecars must initially be
  absent. The test uses one isolated XDG policy and separate runtime state.
- Sole-owner bridge-forwarder cleanup applies to Linux Docker Engine. Desktop,
  Apple and pasta/native strategies do not create that forwarder and skip that
  case. Port 11434 must be free for the deterministic fixture.
- Firewall host-port probes discover the host IPv4 address by a routing lookup.
  Override with `--fixture-host IP` (aggregate: `--security-fixture-host IP`). The
  devcontainer gateway DNS test additionally needs the gateway to be a local
  host address and UDP/53 free and bindable; otherwise it reports a skip.
- The DNS fixture runs only in the disposable Squid container and adds Python
  there. It does not change host DNS. Standard NAT64 address classification is
  covered independently of whether the host has native IPv6.

For a deployment-specific NAT64 path, run the controlled TLS origin on the IPv4
side of that deployment, publish its port as 443, and configure a test DNS name
whose AAAA record uses the deployment's translation prefix. For example:

```bash
uv run python scripts/security_e2e/origin.py --listen 0.0.0.0 --port 8443
# Map deployment TCP/443 to this fixture's TCP/8443, then on the sandbox host:
uv run pytest -ra -q scripts/security_e2e --security-runtime docker \
  -k deployment_nat64 --nat64-origin YOUR_CONTROLLED_FIXTURE_HOSTNAME
```

The fixture never forwards requests elsewhere. Its certificate is temporary;
the probe disables certificate verification only for this controlled origin.
The default security run records this case as skipped unless the topology and
hostname are supplied. A strict result cannot silently count it as covered.

## What remains open

Live Docker, Podman and Apple results for these new cases have not been recorded
by this change. Existing dated runtime results in `development.md` apply only
to the checks described there. Preserve the TODO verification items until the
applicable cases have been run and their evidence reviewed.

Orphan workspace reconciliation, alias/input/mask garbage collection, new Apple
pointer-file support, narrower network policy, query-log redaction and reproducible
tool layers remain implementation work. The tests and expected-limit reports do
not close those roadmap items.
