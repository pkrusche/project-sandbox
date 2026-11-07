# Tasks

Reference implementation: the squid-sidecar line (`jj log -r 'ptyttxkk..rymymutp'`). Port modules from it where a task says so, then adapt them to this design. Unit tests run anywhere; spikes and e2e need a Mac with Apple `container` 1.4.1, Docker Desktop and a Podman machine.

## 0. Spikes (macOS; record results in design.md)

- [ ] 0.1 For each of Apple `container`, Docker Desktop and Podman machine: run a container that publishes `127.0.0.1:3129:3128`, then reach it from two concurrent containers through the firewall-mode local-service strategy for that runtime (`host.docker.internal`). Also check that the port answers on host loopback only. If Apple `container` cannot publish on loopback, record the result and decide the Decision 1 fallback before group 6.

## 1. Policy file (`egress_policy.py`)

- [ ] 1.1 Port `egress_policy.py` and its tests: the `egress.toml` schema (`allow`, `[egress.copilot]`, `[egress.upstream]`, `[egress.ca]`, `[egress.sidecar] port|memory|cpus`), unknown-key rejection, the `[egress.github]` pointer, the GitHub repository host and wildcard rejection, and the effective allowlist with every provider host. Drop `forward_ports`. Verify with `tests/test_egress_policy.py`, including the port range and that `forward_ports` is an unknown key.
- [ ] 1.2 Add the pinned `tomlkit` dependency and the `allow add|remove` edit helpers, re-validated before an atomic replace. Verify with tests that comments and unrelated keys survive and that an invalid edit leaves the file byte-identical.

## 2. Squid config and sidecar image

- [ ] 2.1 Port `squid_config.py`, `templates/egress-sidecar/squid.conf.j2`, the error pages and `sni-match` (design.md Decision 2), including the `client=%>a` log field and `pid_filename`. Verify with `tests/test_squid_config.py` for direct and parent mode, the cause notes and their order, and hostname validation.
- [ ] 2.2 Port `sidecar_image.py`, the `Dockerfile` and `entrypoint.sh` templates and `sbx-reload`, without `socat`, `sbx-forwarders` or `SBX_INTERNAL_SUBNET`, and add the pins to `scripts/update-pins.py`. Verify with renderer tests that every `COPY` source is a rendered file, that the tag changes with any template, and with the update-pins tests.

## 3. Sidecar CA (`session_ca.py`)

- [ ] 3.1 Port `session_ca.py` with the configurable lifetime (1–30 days), the runtime-named CA, the reuse check and the per-session directory with flock and stale removal. Add the pinned `cryptography` dependency. Verify with `tests/test_session_ca.py` for constraints (`github.com`, `invalid`), reuse, expiry, a constraint change, the cap and key file modes.

## 4. Global sidecar orchestration (`egress_sidecar.py`)

- [ ] 4.1 Port the layout, `state.json` handling and fingerprints, adding `port` to the restart inputs and dropping `subnet`, `sidecar_ip` and `forward_ports`. Verify with tests that a garbled `state.json` means "needs up", and that only the restart fingerprint changes with the port.
- [ ] 4.2 Build the sidecar run argv: the default network, `--publish 127.0.0.1:<port>:3128` (or the spike 0.1 fallback on Apple), resource limits, `--log-opt` on Docker and Podman, the `sidecar/` mount. Verify with per-runtime argv tests, including that no `network create` or `network connect` argv exists.
- [ ] 4.3 Port `ensure()`, `down()`, `restart()` and `reload()` without the network and forwarder steps, and add the host-side port-free check before `run`. Verify with injected-runner tests for absent, running-and-ready, stopped, unready and incomplete state; a taken port failing before `run`; two `ensure()` threads issuing exactly one `run`; and `down` deleting `ca-key.pem`.
- [ ] 4.4 Implement drift handling: reload for allowlist differences, a warning naming each restart-only field, and port drift as a launch error. Verify with tests that a reload issues `exec … sbx-reload` and no `rm`/`run`, and that the port case raises an error naming `project-sandbox-egress restart`.
- [ ] 4.5 Best-effort removal of a leftover `sbx-egress` network in `down`, and replacement of a `project-sandbox-egress` container whose state does not match. Verify with runner tests in which `network rm` fails and `down` still succeeds.

## 5. Sidecar-owned relay (Apple, remote upstream)

- [ ] 5.1 Port `egress_relay.py` (foreground `BuildProxyForwarder`, `relay.lock`, `relay.pid`, `relay.port`, `relay.log`) and its supervision in `ensure()`/`down()`, including the port move with re-render and reload. Verify with `tests/test_egress_relay.py`: a CONNECT relayed through a loopback relay, SIGTERM releasing the lock, a pid without a held lock never signalled, and the port-move path.

## 6. `project-sandbox` launch integration

- [ ] 6.1 Add `--egress firewall|sidecar` and the sidecar-mode rejections (`--no-firewall`, chroot, `--extra-domain`, `--internet-proxy`, `--ca-cert`, `--allow-github`, `--copilot-host`) with replacement hints, the Docker Engine and Linux rootless Podman rejection through the existing runtime probes, and the `--agent copilot` profile check. Verify with `tests/test_cli.py` cases asserting each message and that no runner call happens.
- [ ] 6.2 Plan a sidecar-mode sandbox as firewall mode with the sidecar as its Internet proxy: render the `internet-proxy` firewall with the sidecar endpoint and this session's local services, use the firewall-mode proxy environment, start the local-service plans, admit a remote `--agent-proxy` as firewall mode does, and run on the default network. Verify with rendered-firewall, environment and run-argv tests, and that firewall-mode output is byte-identical to before.
- [ ] 6.3 Add the sidecar block to `init-firewall.sh.j2` and the entrypoint: install `ca.pem` and `upstream-ca.pem` from `/project-sandbox-egress` at container start and export the CA variables. Write `sessions/<sid>/sandbox/` with only those files. Verify with renderer tests, a test that the sandbox image checksum does not change when the CA changes, and that the key is never copied.
- [ ] 6.4 Call `ensure()` before the sandbox runs, check the published port against the rendered one and probe it from the host, and add the CA warning and refusal. Verify with tests for port drift, an injected clock (warning, refusal, silence without interception) and an unreachable endpoint.
- [ ] 6.5 Reject launches whose mounts contain `egress.toml` or the state directory, after resolving symlinks. Verify with tests for a mounted `~/.config` and a symlinked mount.
- [ ] 6.6 Port `egress_log.py` and the session-end denial count using the sidecar's clock. Verify with `tests/test_egress_log.py` window tests and a CLI output test, including no output when nothing was denied.
- [ ] 6.7 Show policy, endpoint, drift and would-start/would-reuse in `--dry-run` from a read-only `inspect`. Verify with a dry-run test asserting that only read-only argv were issued and no file was written.

## 7. `project-sandbox-egress` executable

- [ ] 7.1 Port `egress_cli.py` without `forward`, and register it in `[project.scripts]`. Verify that `uv run project-sandbox-egress --help` lists `up`, `down`, `restart`, `status`, `reload`, `allow` and `log`, and that an unknown subcommand exits 2.
- [ ] 7.2 Implement `up`, `down`, `restart [--new-ca]` and `status [--json]` with the published endpoint in the report. Verify with CLI tests using an injected runner, including `status` issuing no mutating argv.
- [ ] 7.3 Implement `allow list|add|remove`, `reload` and `log [--follow] [--denied] [--since]`. Verify with tests for edit plus reload argv, the GitHub rejection with the file unchanged, and both `--since` forms.
- [ ] 7.4 Add the sidecar status line to `project-sandbox doctor`. Verify with `tests/test_doctor.py` that doctor issues only read-only argv.

## 8. Documentation

- [ ] 8.1 Write `docs/egress-sidecar.md` (how it works, policy, parent proxy, managing the sidecar) and add the three-option comparison to `docs/internet-proxy.md`; link from `README.md`. Verify that every documented flag, key and command exists in the parsers and the policy schema.
- [ ] 8.2 Document in `docs/security.md` the sidecar's boundary (the sandbox firewall, no routing isolation over firewall mode, the port open to host processes and to containers reaching host loopback) and the CA key handling. Verify that the documented paths, limits and port match the code constants.
- [ ] 8.3 Record follow-ups in ROADMAP.md and TODO.md: per-sandbox denial attribution, smart GitHub repository access, upstream authentication, the unported DNS probes, and the business/enterprise Copilot capture. Verify that each item names this change.

## 9. End-to-end verification

- [ ] 9.1 Port `scripts/e2e-egress-sidecar.py` and its `run-e2e-tests.sh` wiring to the published endpoint (design.md Decision 15), adding the loopback-only local-service probe and dropping the forwarder checks. Run it on Apple `container`, Docker Desktop and Podman machine and record the results in `docs/development.md`.
- [ ] 9.2 Port the opt-in `copilot-egress` suite and run it with `[egress.copilot] enabled = true`. Record that the Copilot session succeeds and every GitHub repository probe stays denied.

## Workflow follow-up

- Archive this change after review, syncing `egress-sidecar`, `egress-sidecar-management` and `internet-proxy-support`.
- Implement `egress-sidecar-linux` after this change is archived.
