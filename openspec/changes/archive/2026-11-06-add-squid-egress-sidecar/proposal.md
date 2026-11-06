## Why

The default firewall filters Internet access by IP address. `init-firewall.sh` resolves each allowed domain once at startup and pins the answers in an ipset. This is unreliable:

- CDN-backed hosts rotate addresses mid-session.
- Shared IPs admit unrelated virtual hosts.
- Wildcards such as `*.githubusercontent.com` are impossible.
- `--allow-github` opens all of GitHub, even when the agent only needs Copilot.

`--internet-proxy` gives hostname filtering, but it leaves the proxy's lifecycle and policy to the user. On Apple `container`, it conflicts when concurrent sessions use the same proxy. [internet-proxy-locally](https://github.com/pkrusche/internet-proxy-locally) showed that a Squid policy engine with an optional TLS-intercepting CA works well. Its findings also show where Squid needs extra rules: SNI/CONNECT mismatch, raw tunnels, and a denial issued only after the tunnel was established.

## What Changes

- Add an opt-in `--egress sidecar` mode. The default, `--egress firewall`, keeps today's iptables/ipset behaviour unchanged and remains the fallback. Devcontainers stay firewall-only and reject `sidecar`.
- In sidecar mode, every session gets its own Squid sidecar container and its own internal network:
  - **Docker/Podman:** `network create --internal`.
  - **Apple `container`:** `network create --internal`, which is host-only.

  The sandbox joins only the internal network. The sidecar joins both that network and the runtime's egress network. The sandbox's iptables rules shrink to "the sidecar IP only": its proxy port plus the enabled local-service ports, which the sidecar forwards to the host (the agent gateway and Ollama).
- **Hostname policy in Squid.** Squid checks every CONNECT against a hostname allowlist (`dstdomain`, wildcards allowed) and peeks at the TLS SNI. It terminates the connection when the SNI is not allowlisted or differs from the CONNECT host, and it denies non-TLS bytes in a tunnel. `--extra-domain` and the built-in provider, GitHub and Copilot sets become Squid ACLs instead of pinned IPs.
- **GitHub repository access is blocked.** GitHub repository hosts (git, REST, raw, archives, gists, release assets) cannot be allowlisted in sidecar mode, `--allow-github` is rejected, and per-repository read/write access is moved to the roadmap.
- **Copilot host profile.** A Copilot-only allowlist covers the Copilot service hosts, so the Copilot CLI works while GitHub repository access stays blocked. If Copilot needs its token-exchange paths on `api.github.com`, that host alone is intercepted and only those paths pass. A billable e2e capture (`copilot-egress`, task 1.3) decides this, and confirms the host set.
- **Minimal TLS interception.** At most `api.github.com` is intercepted, for the Copilot token exchange. All other allowed hosts are spliced untouched.
- **Per-session CA.**
  - A fresh CA is generated on the host for each session, name-constrained to the intercepted host's registrable domain (`github.com`; Squid mimics GitHub's `*.github.com` certificate names). With nothing intercepted, it is constrained to the reserved name `invalid`, because Squid's peek/splice port still needs one.
  - Its private key is mounted read-only only into the sidecar and deleted at teardown.
  - When a host is intercepted, the public certificate is mounted into the sandbox and installed into system trust by the root phase of the entrypoint at startup. It is not baked into the image, so the image cache stays valid.
- **Host-side policy file.** `--egress-policy PATH` (default `$XDG_CONFIG_HOME/project-sandbox/egress.toml`, if present) holds hostnames and the Copilot profile switch. Paths inside the project or worktree are rejected, so an agent cannot widen its own policy for the next session.
- **Optional parent proxy.**
  - In sidecar mode, `--internet-proxy URL` becomes Squid's `cache_peer` parent (`never_direct`), so Squid can chain to a corporate proxy that has its own CA.
  - `--ca-cert` inputs are added to Squid's outgoing trust as well as the sandbox image.
  - On Apple `container`, Squid reaches the parent through the existing host relay, which now uses a per-session dynamic port in sidecar mode.
  - In parent mode, policy is hostname-only. Squid performs no local DNS-based IP checks.
- **Distinct denial pages.** Our denials are labelled distinctly from upstream proxy denials. The CLI prints a short summary of blocked hosts at session end.
- **Sidecar lifecycle.** The CLI generates config, starts the sidecar and waits until it is ready. It tears down the sidecar, network and CA on exit. Orphans left by a crash are removed on the next launch.

## Capabilities

### New Capabilities
- `egress-sidecar`: the opt-in Squid sidecar egress mode. Covers mode selection, topology per runtime, hostname and SNI policy, selective interception and the per-session CA, GitHub repository blocking, the Copilot profile, the host-side policy file, upstream parent proxy chaining, lifecycle, observability and verification.

### Modified Capabilities
- `internet-proxy-support`: in sidecar mode, `--internet-proxy` is the sidecar's upstream parent rather than a direct sandbox endpoint, and `--extra-domain` / `--egress-policy` are accepted because the sidecar owns local policy (`--allow-github` is rejected in sidecar mode). Firewall-mode behaviour is unchanged.

## Non-goals (deferred to TODO.md)

- Authentication to the upstream proxy (Basic, NTLM, Kerberos). HTTP 407 continues to fail the request.
- Credential injection by the sidecar (for example, adding a GitHub token to allowed repository paths so the sandbox holds no token).
- Request-body inspection, DLP and ICAP.
- Per-repository GitHub access (separate read/write rules, LFS, release assets). Moved to ROADMAP.md.
- Transparent interception of clients that ignore proxy variables. They fail closed, as today.
- Sidecar mode for devcontainers and chroot.
- Replacing firewall mode as the default.

## Impact

- **Code**:
  - New modules: `egress_sidecar.py` (lifecycle, network and container argv), `egress_policy.py` (policy file loading and validation), `session_ca.py` (CA generation and name constraints).
  - New templates: `squid.conf.j2` and the sidecar `Dockerfile`/entrypoint.
  - Changed: `cli.py` (flags, validation, orchestration), `container_cli.py` (network and mount argv), `firewall.py` + `init-firewall.sh.j2` (new `sidecar` policy), `entrypoint.sh.j2` (runtime CA install and per-client CA environment), `internet_proxy.py` (environment target and relay port), `doctor.py` (runtime network capability checks).
- **Images**: a new pinned Squid sidecar image (Squid 6.x with `security_file_certgen`), registered in `scripts/update-pins.py`.
- **Tests**:
  - Unit: policy parsing, rendered `squid.conf`, CA constraints, argv construction for each runtime, dry-run.
  - New opt-in e2e suite `egress-sidecar`, which ports the relevant upstream probes: SNI mismatch, raw tunnel, metadata, GitHub repository blocking, the Copilot profile, and parent chaining.
  - New opt-in e2e suite `copilot-egress` (billable) that captures the Copilot CLI's hosts and paths and verifies the Copilot-only profile.
- **Docs**: a new `docs/egress-sidecar.md`, plus updates to `docs/security.md`, `docs/internet-proxy.md`, `docs/proxy-endpoints.md`, `ROADMAP.md`, `TODO.md` and `CHANGELOG.md`.
- **Security-sensitive**:
  - A new CA private key on the host per session.
  - New trust injected into the sandbox at runtime.
  - Firewall rules change in the new mode.
  - The policy file sits outside the project.
- **Related change**: `add-copilot-agent-support` makes `--agent copilot` imply `--allow-github`. In sidecar mode, `--agent copilot` instead enables the Copilot profile only, and `--allow-github` is rejected.
