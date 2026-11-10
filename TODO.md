# TODO - outstanding items for next release

- `--build-only --pi-ollama` builds an image without the Ollama firewall rule.
  `--build-only` rejects `--agent`, and the Pi + Ollama setup is only enabled when
  `--agent pi` is also given (`cli.py`, `bool(args.pi_ollama and run_agent == "pi")`).
  A later `--agent pi --pi-ollama --no-build` session then reuses that image, and
  Ollama is refused with "No route to host". Either let `--build-only` honour
  `--pi-ollama` (or accept `--agent`), or have `--no-build` detect an image
  rendered without the session's Ollama settings.
- Manually verify that `kill -9` of the only `--pi-ollama` session releases the
  shared bridge forwarder (it should exit within about one poll interval) and
  that a fresh session then attaches cleanly. The concurrent-session `kill -9`
  case was verified on 2026-11-08 (see docs/development.md).

## Security review

Reviewed 2026-11-08 against parent revision `595c1432`. Scope: command and
configuration quoting, host/container trust boundaries, locks and concurrent
sessions, and firewall/sidecar allowlists. This report proposes changes; it does
not implement them.

The highest priorities are privileged Python imports from the workspace,
host-side Git execution against agent-writable metadata, unsafe generated-path
handling, and races that can substitute a different session's network policy.
Passing unit tests do not currently establish these boundaries.

### Findings and suggested fixes

#### 1. High — firewall Python helpers import code from the writable workspace

**Addressed:** privileged Python helpers now use `python3 -I`, and the script
changes to `/` with a fixed system `PATH` before initialization. A root-owned
atomic guard rejects repeated/concurrent attempts, including retries after
failure. Successful initialization removes the dedicated sudoers entry; the
installed script is root-only. Containers must be recreated from the image.
Regression tests exercise workspace module shadowing and the one-time lifecycle.
Live runtime verification of sudo revocation remains outstanding. The original
finding below describes the reviewed revision, before these changes.

**Locations:** `src/project_sandbox/templates/init-firewall.sh.j2:65` and
`:346`; `templates/_resolve_endpoints.py.j2:1`;
`templates/Dockerfile.j2:135`; `container_cli.py:403–404`.
Paths abbreviated below are also relative to `src/project_sandbox/`.

The container works in `/workspace`, and the agent may invoke the firewall
script through passwordless sudo. Both Python helpers use `python3 -`, which
searches the current directory for imports. A project containing `ipaddress.py`,
`socket.py`, or another imported module can therefore execute code as root
inside the container. Proxy and sidecar configurations reach these helpers.
An agent can also create such a module and invoke the permitted sudo command
again. Root execution can defeat the firewall through the granted `NET_ADMIN`
capability; this is not merely a startup denial of service.

**Evidence:** a harmless temporary `ipaddress.py` raising a recognizable
`SystemExit` executed when running `python -` in that directory. The same
command with `-I` imported the standard library successfully. The privilege
transition is established by the generated sudoers rule; a live container
exploit was not run. Python documents the import-path behavior and isolated
mode in its [command-line reference](https://docs.python.org/3/using/cmdline.html).

**Suggestion:** run every privileged Python helper with `python3 -I`, use a
trusted working directory, and audit indirect commands for similar dependence
on repository files. Add a regression that places shadow modules in a temporary
workspace and runs the rendered helper, plus a runtime test that verifies the
agent cannot obtain root through the allowed firewall command.

#### 2. High — automatic Git finalization can execute agent-controlled code on the host

**Locations:** `cli.py:3066–3086`; `worktree.py:136–194`;
`jj_workspace.py:185` and its Git-backend mount helpers.

Git branch sessions expose the main repository's `.git` read-write. The host
then runs Git status, add, and commit against the session worktree during
finalization, without disabling hooks or otherwise isolating repository config.
The agent can modify `.git/hooks`, set `core.hooksPath`, or configure executable
helpers. For example, a pre-commit hook planted in the shared metadata can run
as the host user when the automatic commit captures a dirty worktree. No merge
or push is needed. Git explicitly supports executable commit hooks and
configurable hook locations: [Git hooks](https://git-scm.com/docs/githooks).

This is a code-path finding, not a demonstrated host compromise. The existing
ROADMAP recognizes shared-store concurrency risk, but the host execution risk
also makes metadata isolation a security priority. Normal writable checkouts
and the jj Git-backend mount likewise allow poisoning later host operations.

**Suggestion:** prioritize independent clones with private VCS metadata. Capture
changes inside the sandbox and import reviewed objects through a trusted host
repository. As an interim measure, disable hooks for host operations and audit
all executable Git configuration surfaces, including filters, fsmonitor, and
signing. `core.hooksPath=/dev/null` alone is not a complete defense against
attacker-writable Git configuration. Test with inert marker hooks/helpers and
confirm none execute on the host.

#### 3. High — generated-file writes follow repository-controlled symlinks

**Locations:** `dockerfile.py:174–262`, `:1134–1146`; `firewall.py:174`;
`config_agents.py:146–154`, `:534–556`; `devcontainer.py:50–70`, `:268`;
`cli.py:3378–3388`.

Several renderers use `write_text()` on predictable repository paths without
checking the final file or safely opening its parent. The agent-config guard
checks directory components, but not leaf files such as `settings.json` and
`config.toml`. Dockerfile/firewall generation happens before that guard runs.
The root `.gitignore` is also agent-writable and is subsequently rewritten by
the host. Symlink planting can redirect these writes outside the project.

**Evidence:** temporary symlinks at `.project-sandbox/init-firewall.sh` and
`.project-sandbox/claude/settings.json` both caused the corresponding renderer
to overwrite a harmless file outside the temporary project.

**Suggestion:** validate all managed directories before the first mutation;
create/write files through verified directory descriptors with no-follow
semantics. Reject symlinked leaves or replace them atomically without following
their targets. Apply the same policy to `.devcontainer`, `.gitignore`, logs,
certificates, and cleanup paths. A separate `is_symlink()` check is insufficient
against concurrent replacement. Cover both pre-existing links and link swaps.

#### 4. High — implicit history and mask mounts can expose arbitrary host directories

**Locations:** `paths.py:30–63`; `cli.py:3205–3237`;
`devcontainer.py:97–110`.

`ensure_history_paths()` and `ensure_workspace_sandbox_mask()` accept existing
symlinked directories. Launch code then resolves and mounts them. A repository
with `.project-sandbox/history/shell` pointing outside the project can cause an
unexpected writable host-directory mount in an interactive session. A
`workspace-mask` symlink can expose host files even in a headless session: it
becomes the supposedly empty read-only directory over `/workspace/.project-sandbox`.
Read-only still permits disclosure.

**Evidence:** temporary history and mask symlinks to an outside directory were
accepted by both helpers and resolved to that directory. No container was
started. This does not require an explicitly authorized extra `--mount`.

**Suggestion:** use a fresh, private, verified empty directory outside the
project for masks. Reject symlinked or unexpected history sources and validate
every implicit mount source, including ownership and traversal. Test final
constructed mounts, not just the helper return values.

#### 5. High — valid extra mount options bypass sidecar policy/key isolation checks

**Locations:** `container_cli.py:153–189`; `cli.py:1530–1555`.

`parse_mount()` preserves an unmodeled mount as `raw`, including a normal bind
mount with `bind-propagation=rprivate`. `_egress_sidecar_policy()` checks mount
sources only when `spec.raw is None`. Consequently, an extra option causes an
otherwise prohibited policy or state directory to bypass the exclusion check.
The runtime still receives the raw mount. Exposing the policy can let an agent
widen future/live policy; exposing sidecar state can disclose the CA private key.

**Evidence:** a temporary policy-directory bind was rejected normally but
accepted after appending `,bind-propagation=rprivate`. This requires the launch
to include that mount; an agent cannot introduce a host CLI flag by itself.
It nevertheless contradicts the promised check of every mount source.

**Suggestion:** inspect known source paths even for passthrough mounts, and
reject forms whose filesystem exposure cannot be determined in sidecar mode.
Use the runtime's actual mount grammar rather than a partial comma split.
Cover mount aliases, additional options, quoted fields, and indirect volume
forms that can expose host directories.

#### 6. High — the build lock does not protect generated policy or the image selected at launch

**Locations:** `cli.py:744–840`, `:907–986`;
`build_cache.py:35–44`.

Dockerfiles, entrypoints, firewall scripts, and agent configs are rewritten
before acquiring the build lock. Two launches for one context with different
allowlists can overwrite each other's inputs while one build is reading them.
The fingerprint and resulting image can consequently describe different input
versions. After the build lock is released, another launch can also replace
the shared image tag before the first launch starts its container. A run that
requested a restrictive policy may launch a more permissive image.

This follows from the lock boundaries; a timed runtime race was not executed.
The same overlap can put a proxy secret written after one build into another
build's shared context.

**Suggestion:** render immutable, per-session build contexts; fingerprint the
complete context and launch by the resulting immutable image ID/digest. If
shared contexts remain, protect rendering through build completion, and
separately prevent tag replacement from changing what a session launches.
Include runtime/image identity in resource locking where appropriate. Add a
barrier-controlled test with two different network policies.

#### 7. High — allowlist edits can lose changes or restore a revoked host

**Locations:** `egress_cli.py:284–308`; `egress_policy.py:578–626`;
`egress_sidecar.py:920–945`, `:1395–1420`.

Policy editing reads, modifies, and atomically replaces the TOML file without
a lock. Only the subsequent sidecar reconciliation is locked, and that lock is
per runtime. Atomic replacement prevents a partial file, but not lost updates.
Also, `ensure()`/`reload()` receive an already loaded policy: a delayed launch
can acquire the lock after a newer policy has been applied and reapply its stale
allowlist, including a host just removed by the user.

**Evidence:** two temporary policy additions synchronized immediately before
replacement both returned success, but the final policy retained only one host.
The stale live-policy scenario is established by the call sequence, not a live
Squid test.

**Suggestion:** introduce one lock keyed by the global policy path, independent
of runtime. Hold it across read/validate/edit/write and coordinate application
with runtime locks in a fixed order. Reload current policy under that protocol
instead of trusting an earlier snapshot; use generations/hashes to prevent
older applications from winning. Test concurrent add/remove and a delayed
launch following a revocation.

#### 8. Medium — credentials, proxy configs, and prompts are shared across live sessions

**Locations:** `config_agents.py:254–268`, `:573–581`;
`copilot_auth.py:198–220`; `cli.py:817–824`, `:1219–1227`, `:3166–3195`.

Credential staging is keyed by project and agent rather than session. Copilot
launches replace the same token file, and either session's cleanup removes it.
`--no-forward-credentials` purges another live session's staging tree. Prompt
directories are deleted/recreated at fixed paths, and proxy configs share
filenames that other launches rewrite or unlink. Read-only container mounts do
not make their host sources immutable. Depending on timing, a session can see
another prompt/configuration/token or fail before provisioning.

**Suggestion:** use private per-session staging directories, mount only that
session's inputs, and clean up only resources it owns. Keep persistent
devcontainer staging separate. Use session leases/reference counting for truly
shared resources. Add tests where one session starts or exits while another is
waiting to read its prompt and credentials.

#### 9. Medium — workspaces lack lifetime ownership, and Git locks are deleted without proving staleness

**Locations:** `worktree.py:37–78`, `:132`, `:156`, `:212–217`;
`jj_workspace.py:62–78`, `:141`, `:287–298`.

Both backends reuse an existing registered workspace for the same branch or
bookmark without checking for an active session. Setup/finalization locking
does not prevent two agents from sharing that workspace for their full runs;
one finalizer can remove it while the other is using it. Git setup additionally
does not take the teardown lock. `_clear_stale_index_lock()` unconditionally
unlinks an existing `index.lock`; the host advisory lock does not establish that
another Git process, editor, or container has stopped using it. Its metadata
path is guessed from the worktree basename rather than queried from Git.

**Suggestion:** acquire a workspace lease for the complete session or reject
concurrent use of the same workspace. Use one repository mutation lock for Git
setup and finalization. Do not automatically remove an index lock merely because
it exists; fail with recovery guidance unless ownership and termination are
established. Resolve the actual worktree metadata location. Test overlapping
same-branch sessions and a live external Git lock.

#### 10. Medium — Cargo workspace paths are interpolated into shell and Dockerfile syntax

**Locations:** `cli.py:2615–2644`; `dockerfile.py:1017–1034`.

Cargo member paths discovered from the repository are inserted unquoted into
`mkdir`, `touch`, and `rm -rf` shell commands and space-delimited `COPY`
instructions. Spaces break the generated build; shell substitutions and
metacharacters can execute unintended commands as the build user, normally
root. This is build-container execution, not demonstrated host execution.
Rust builds already execute project build scripts, but the filename injection
is an additional defect and affects benign paths too.

**Evidence:** a temporary Cargo workspace member named `crate$(id)` was accepted
and rendered into `RUN mkdir -p crate$(id)/src`. Only rendering was performed;
the injected shell command was not executed.

**Suggestion:** render `COPY` with JSON arrays; quote each shell path with
`shlex.quote` and use option terminators. Reject control characters/newlines and
members escaping the project after resolution. Test spaces, quotes, dollar
signs, semicolons, leading hyphens, and symlinks. Validate image/version strings
before inserting them into `FROM` instructions as well.

### Networking allowlist recommendations

These are policy limitations and hardening suggestions, distinct from the
implementation defects above. Some are already described in `docs/security.md`.

| Area | Current behavior and recommendation |
| --- | --- |
| Firewall-mode destination identity | `templates/init-firewall.sh.j2:168–225,316–317` resolves names once and admits the resulting IPs. The final rules have no protocol or destination-port restriction, apart from earlier DNS drops. Any service/virtual host on those IPs is potentially reachable. Restrict normal service rules to required TCP ports, maintain explicit exceptions, and use sidecar mode where hostname enforcement is required. Do not describe IP pinning as a hostname guarantee. |
| Private addresses in normal domain pins | The normal A/AAAA loop adds DNS answers without excluding loopback, RFC1918, link-local, metadata, or other special-use destinations. A compromised resolver or an explicitly added domain resolving internally can open unintended internal services on all ports. Apply address classification before inserting pins and require a separate explicit private-service exception with exact ports. Keep intentional remote corporate proxy endpoints distinct from public provider policy. |
| GitHub/Copilot breadth | `--allow-github` imports broad web/API/git CIDRs and adds repository, registry (`ghcr.io`), Copilot, and telemetry hosts. Selecting Copilot implies this broad access in firewall mode. Separate Copilot inference/entitlement, repository access, registries, and telemetry into independently enabled profiles; prefer the existing restricted sidecar Copilot profile where supported. Keep the warning about broad GitHub-token scope. |
| Default providers | `egress_policy.effective()` always adds all seven provider hosts, even for a gateway-backed session; users cannot remove them. Normal firewall mode also admits both provider families unless an agent proxy suppresses them. Allow an explicitly restrictive policy with no implicit providers, or isolate sidecars by policy/profile if per-session restrictions are needed. Make the global-union semantics visible in previews. |
| Devcontainer gateway | `templates/init-firewall.sh.j2:244–258` admits every port/protocol to and from the gateway. This exposes host services and precedes the port-53 drops, so it can also permit gateway DNS despite the general-DNS-block claim. Make broad gateway access opt-in; list the required IDE connections and implement narrower, stateful rules. Test DNS and unexpected host ports in this mode. |
| Sidecar TLS scope | The sidecar checks CONNECT host against ClientHello SNI, then splices most connections. It cannot inspect encrypted HTTP Host/`:authority`, paths, methods, or payloads on those connections. That follows from [Squid's documented splice behavior](https://www.squid-cache.org/Doc/config/ssl_bump/). Document this limit; test domain fronting with a controlled origin that supports it. This review did not establish that a currently allowlisted provider supports fronting. |
| Sidecar HTTP | `templates/egress-sidecar/squid.conf.j2` permits plain HTTP on port 80 for most allowed hosts, including providers. Prefer HTTPS-only defaults with explicit HTTP exceptions to reduce plaintext leakage and origin impersonation. |
| Wildcards and GitHub exclusions | `*.example.com` also matches the apex after Squid conversion. Two-label validation still admits broad public-suffix patterns such as `*.co.uk`. Reject public-suffix wildcards or require explicit acknowledgement. The finite repository-host denylist is not a complete GitHub-content classifier: `lfs.github.com` is in the firewall list but absent from `GITHUB_REPOSITORY_HOSTS`, and user content can live on other domains. Clarify the guarantee and test every officially supported repository transport/host before claiming all repository access is blocked. |
| Copilot interception | `/copilot_internal/` permits any matching path and method. Narrow to observed entitlement endpoints/methods where practical. Add live fixture tests for dot segments, percent encoding, conflicting Host/CONNECT/SNI, and redirects. These are validation tasks, not confirmed path bypasses. |
| Private destinations and upstream parents | Direct Squid mode has a useful private-address denylist; upstream mode deliberately omits it and trusts the parent for destination resolution. Require the parent to enforce equivalent internal/metadata restrictions when those are part of the threat model. Test mixed public/private DNS answers, rebinding, IPv4-mapped IPv6, and deployment-specific NAT64 paths. |
| Startup, reinitialization, and revocation | Reinitialization is now rejected by an atomic one-attempt guard, and successful initialization revokes sudo access. The initial installation still flushes rules before installing final DROP policies and allows ESTABLISHED/RELATED traffic; sidecar reloads also need explicit semantics for existing tunnels. Install policy atomically where possible and distinguish blocking new connections from terminating existing ones after revocation. Audit broadly allowed outbound ICMPv6 error types alongside the actual capabilities of the agent process. No packet-level bypass was tested here. |

### Additional hardening

- **Mount encoding:** `_mount_arg()` and devcontainer mounts concatenate paths
  into comma-delimited strings. JSON escaping and an argv list do not escape
  the runtime's nested mount syntax. Encode fields according to each runtime's
  grammar, or reject unsupported commas/control characters explicitly. Verify
  parse/render round trips for unusual project names. Docker documents the
  outer CSV parsing requirement in its [mount documentation](https://docs.docker.com/engine/storage/volumes/).
- **Host lock files:** `host_locks.path_lock()` correctly uses `O_NOFOLLOW` and
  avoids truncation, but does not verify the opened inode is a regular file
  owned by the caller. Names are predictable in shared temporary storage. Use a
  verified private per-user lock directory, `fstat()` checks, and bounded waits
  with useful diagnostics. Existing OS protections may limit cross-user attacks;
  do not depend on identical behavior across Linux and macOS. Never unlink a
  live lock file to recover it, since that can create two independently locked
  inodes.
- **Secrets and logs:** create proxy configuration files with mode 0600 from
  the initial open instead of writing and then chmodding. Keep them outside all
  build contexts. Explicit API-key env files should have session-scoped cleanup
  too. Squid's `%ru` logging includes URLs/query strings for visible HTTP and
  intercepted requests; redact sensitive fields and define retention/access
  rules. Token substitution in logs is useful but is not a general data-loss
  prevention boundary.
- **Supply chain:** exact top-level npm versions are useful, but global
  `npm install -g package@version` does not freeze all transitive dependency
  resolution. Use reviewed lockfiles/integrity metadata or reproducible tool
  layers, and define a deliberate image/security-update cadence. Project build
  scripts and user-supplied Dockerfiles remain trusted image-producing code.

### Existing defenses worth preserving

Subprocess construction generally uses argv lists. Extra domains and proxy
authorities receive strict validation, shell domain values use `shlex_quote`,
and devcontainer JSON uses a serializer. The endpoint helper validates returned
addresses before constructing shell arrays, so its `eval` is not an obvious
input-injection finding; its unsafe import path is the concrete problem.
Selected-agent credential filtering, private credential staging, no-follow
exclusive creation of several secret files, constrained sidecar CAs, and the
sidecar's deny-before-CONNECT/SNI checks are useful protections. The chroot
backend is explicitly documented as a layout test rather than a security
boundary and was assessed accordingly.

### Verification and limits

- `uv sync` completed.
- `uv run python -m compileall -q src tests` passed.
- `uv run project-sandbox --help` passed.
- `uv run pytest -q`: **1295 passed, 4 skipped, 882 subtests passed**.
- Temporary, non-destructive probes reproduced Python import shadowing,
  generated-file symlink writes, symlinked implicit mount sources, Cargo path
  interpolation, the raw-mount policy-check bypass, and lost policy edits.
  These probes used synthetic files/values and left no repository changes.
- No runtime containers, live credential use, host hook payloads, or external
  exploitation were exercised. Network behavior still needs adversarial
  end-to-end coverage on Apple container, Docker, and Podman. This was a source
  review, not a dependency-CVE audit or proof that no other issues exist.

Suggested order: fix findings 1–5 first, then make build inputs, policy updates,
and session staging concurrency-safe (6–9), fix quoting (10), and validate the
narrower networking profiles with runtime tests.
