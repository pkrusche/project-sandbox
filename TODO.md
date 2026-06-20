# TODO — outstanding items

## Firewall: verify multi-resolver rules on a real iptables host
  
Code is **complete**: `init-firewall.sh.j2` now collects all IPv4/IPv6
nameservers via `mapfile` and emits NAT/ACCEPT rules per resolver (no more
`{print $2; exit}` for DNS), with a `127.0.0.11` fallback; README says
"resolver(s)"; `tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`
covers the rendered script. The unit tests are render-only by policy and do
**not** exercise live iptables. Outstanding: run the rendered script on a host
with iptables (multiple `nameserver` entries in `resolv.conf`) and confirm DNS
to every resolver is permitted and nothing else leaks before treating this as
shipped — it is the network security boundary.

## Repository review findings (2026-06-19)

These came from a critical multi-agent pass over `src/`, `tests/`,
templates, scripts, and repository metadata. Add regression tests with each fix.

### High

- **Escape and validate `--extra-domain` before rendering the firewall.**
  `src/project_sandbox/templates/init-firewall.sh.j2` inserts each extra domain
  into a root-run Bash array inside double quotes. Values containing command
  substitutions, backticks, embedded quotes, or newlines can execute during
  firewall initialization. Validate domain syntax in the CLI/render path and
  render with shell-safe quoting; add tests for `$(...)`, backticks, and quotes.

- **Render `devcontainer.json` with real JSON escaping.**
  `src/project_sandbox/templates/devcontainer.json.j2` interpolates project
  names, Git identity values, paths, and `--mount` values directly into JSON
  strings. Quotes/newlines can break the file or inject additional devcontainer
  fields such as lifecycle commands or mounts. Prefer building a Python dict and
  `json.dumps`, or apply `tojson` to every string/list field; add tests with
  quotes/newlines in project names, identities, and extra mounts.

- **Do not mount a prompt file's whole parent directory.**
  `src/project_sandbox/cli.py` mounts `prompt_file.parent` for `--prompt FILE`.
  Passing a prompt from `$HOME` exposes the whole home directory read-only to the
  agent. Copy prompt files into a private generated/staging directory and mount
  only that directory; update the current test that expects parent-directory
  mounting.

- **Reject symlinked generated config/credential paths under `.project-sandbox`.**
  `config_agents.render()` and stale credential cleanup write/remove paths such
  as `.project-sandbox/claude/settings.json` without first rejecting symlinked
  parents. A repository can pre-place symlinks so rendering writes outside the
  project, and stale cleanup can delete host credential files through a symlinked
  `.project-sandbox/claude`. `lstat` managed path components before writing or
  deleting, reject symlinks, and add hostile-repo regression tests.

- **Close firewall egress bypasses through DNS and broad ICMPv6.**
  The firewall keeps DNS open to the resolver for arbitrary names, allowing DNS
  query exfiltration to attacker-controlled domains. It also allows all outbound
  `ipv6-icmp`, which can carry data to arbitrary IPv6 hosts when IPv6 is
  available. Move DNS through a constrained resolver/proxy or pre-resolve
  allowed domains before blocking general DNS; restrict ICMPv6 to required
  neighbor-discovery/PMTU types and scopes.

- **Fix the ignored-lockfile build contract.**
  The root `Dockerfile` copies `uv.lock` and runs `uv sync --frozen`, and the
  README documents the same pattern, but `.gitignore` ignores `uv.lock` and it is
  not tracked. A clean checkout cannot build the documented image. Either commit
  `uv.lock` and stop ignoring it, or remove the lockfile/`--frozen` requirement
  from the Dockerfile and docs.

### Medium

- **Terminate headless containers on parent interruption, not just timeout.**
  `session.run()` tears down the process group only for `TimeoutExpired`.
  `KeyboardInterrupt` or another parent exception can leave a named headless
  container running because `start_new_session=True` isolates it from terminal
  signals. Add a `BaseException` cleanup path that terminates the group/container
  when `proc.poll() is None`, then re-raises.

- **Validate fatal CLI inputs before creating a worktree.**
  Worktree setup currently happens before build-source and prompt validation.
  Typos such as a missing `base_image`, bad `--dockerfile`, or missing `--prompt`
  can create a branch/worktree before failing. Validate build source, prompt
  path, log path parent, and other fatal inputs before `_setup_worktree()`, or
  track newly-created worktrees and remove them on pre-session failures.

- **Run `gh pr create` from the target repo and surface failures.**
  `worktree.teardown(..., after="pr")` invokes `gh pr create` without `cwd=repo`
  and ignores the exit status. Launching project-sandbox from another directory
  can target the wrong repo or fail silently. Run with `cwd=str(repo)`,
  `check=True`, and leave the worktree in place with a clear message on failure.

- **Narrow the devcontainer host-network firewall exception.**
  The devcontainer firewall variant allows the entire IPv4 interface CIDR from
  `HOST_NET4`, not just the host gateway. That can expose peers on the container
  subnet. Match the IPv6 behavior by allowing only the default gateway, ideally
  with specific attach/IDE ports.

- **Pin or verify mutable remote installs in generated Dockerfiles.**
  The templates install root-level tooling with `curl | bash`, `latest` tags,
  GitHub latest release discovery, and unpinned global npm packages. A compromised
  upstream or bad latest release yields an image that later receives workspace
  and agent credentials. Pin versions/digests, verify checksums, and make
  upgrades explicit.

- **Handle `FROM --platform=...` when warning about non-apt base images.**
  The Dockerfile parser treats the first token after `FROM` as the image, so
  `FROM --platform=$BUILDPLATFORM alpine:3.19` is parsed as `--platform=...` and
  skips the Debian/Ubuntu warning. Parse and skip `FROM` options before extracting
  the image; add a regression test.

- **Do not silently drop mixed Dockerfile `RUN` blocks.**
  `_remove_restricted_user_setup()` removes an entire `RUN` instruction whenever
  it contains `useradd`, `groupadd`, or similar. Mixed commands such as
  `apt-get install ... && useradd app && make setup` lose unrelated setup work.
  Reject mixed user-management blocks with an actionable error, or rewrite only
  the restricted subcommands.

- **Align timeout documentation with the actual stop behavior.**
  README and script comments describe a graceful stop path, while
  `build_stop_argv()` uses runtime `kill`, i.e. immediate SIGKILL. Decide the
  intended contract. If graceful stop is required, use bounded `stop` and test
  `build_stop_argv()` directly; otherwise update README, `session.py`, and
  script comments to say timeouts kill immediately.

- **Make the e2e smoke test cover all documented devcontainer artifacts.**
  `scripts/e2e-test.sh` claims every artifact is checked, but omits
  `.project-sandbox/Dockerfile.devcontainer`,
  `init-firewall-devcontainer.sh`, `claude-devcontainer/settings.json`, and
  `codex-devcontainer/config.toml`; symlink checks only assert `-L`, so dangling
  links can pass. Add the missing artifacts and verify symlink targets exist, or
  soften the wording to "key artifacts."

### Low

- **Avoid default session log collisions.** Default log filenames use
  second-level timestamps and are opened with `"w"`, so two same-agent sessions
  started in one second can overwrite each other. Add higher-resolution time,
  UUIDs, or exclusive-create/retry logic.

- **Make devcontainer history initialization robust to apostrophes in paths.**
  `devcontainer.py` builds `initializeCommand` using single-quoted
  `${localWorkspaceFolder}` paths. Workspace paths containing `'` can create the
  wrong directories and later break bind mounts. Prefer array-form lifecycle
  commands or a non-shell-quoted mechanism.

- **Warn on `npm ci` in non-`/workspace` Dockerfiles.** The local-install warning
  regex handles `npm install` but not `npm ci`, so Node projects can miss the
  WORKDIR mismatch warning. Add `npm ci` coverage.

- **Make the timeout teardown verifier target only its own container.**
  `scripts/verify-timeout-teardown.sh` diffs all running container IDs before and
  after the run, so unrelated containers started during the settle window look
  like leaks. Filter by a known name/label or expose/capture the generated
  project-sandbox container name.
