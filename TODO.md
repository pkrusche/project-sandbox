# TODO — outstanding items

Each item below carries a short **Problem** statement, an **Implementation
strategy** naming the real files/functions to touch, and a **Test** line for the
regression coverage to add (per CLAUDE.md, every fix ships with a test). File
references are `path:line` against the current tree.

## Firewall: verify multi-resolver rules on a real iptables host

**Problem.** Code is **complete**: `init-firewall.sh.j2` collects all IPv4/IPv6
nameservers via `mapfile` and emits NAT/ACCEPT rules per resolver (no more
`{print $2; exit}` for DNS), with a `127.0.0.11` fallback; README says
"resolver(s)";
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`
covers the rendered script. Unit tests are render-only by policy and do **not**
exercise live iptables.

**Implementation strategy.** No source change expected — this is a manual
validation gate. On a Linux host with `iptables`/`ip6tables` and a
`/etc/resolv.conf` containing multiple `nameserver` entries (mix IPv4 and IPv6),
run the rendered `.project-sandbox/init-firewall.sh` as root and confirm DNS to
every listed resolver is permitted and nothing else leaks. This is the network
security boundary, so treat it as unshipped until exercised live.

**Test.** Render coverage already exists; record the live-host result (resolvers
allowed, no other egress) in the PR description. If the run reveals a gap, add a
render regression in `tests/test_renderers.py` for the corrected rule shape.

## Repository review findings (2026-06-19)

These came from a critical multi-agent pass over `src/`, `tests/`, templates,
scripts, and repository metadata.

### High

- **Escape and validate `--extra-domain` before rendering the firewall.**
  - *Problem.* `init-firewall.sh.j2:99-110` interpolates each extra domain raw
    into a root-run Bash array (`"{{ domain }}"`). The Jinja environment has no
    autoescape and no custom filters (`templating.py:9`), and neither
    `cli.py` (`--extra-domain`, `cli.py:79`, passed at `:178`) nor
    `firewall.render()` (`firewall.py:6-33`) validates the values. Command
    substitutions, backticks, embedded quotes, or newlines execute during
    firewall init.
  - *Implementation strategy.* Validate each domain against a strict hostname
    regex (letters/digits/hyphens per label, dots between, optional trailing
    dot; reject everything else) in the `cli.py`/`firewall.py` render path and
    raise on rejection. As defense in depth, register a `shlex_quote` Jinja
    filter in `templating.py` and apply it in the template, or pre-quote the
    list in `firewall.py` before `tmpl.render(...)`.
  - *Test.* In `tests/test_renderers.py`, assert `firewall.render()` rejects
    `$(...)`, backticks, embedded `"`, and newlines, and that a valid domain
    still renders quoted.

- **Render `devcontainer.json` with real JSON escaping.**
  - *Problem.* `devcontainer.json.j2` interpolates `project_name`, `user_name`,
    `user_email`, history mounts, and `extra_mounts` (the `--mount` values,
    `cli.py`→`devcontainer.py`) directly into JSON strings with no escaping
    (`templating.py:9`). `--mount` is the worst case: it is rendered as a bare
    array element, so a crafted value can close the string/object and inject
    additional devcontainer fields (lifecycle commands, extra mounts). Quotes or
    newlines in identity/name also corrupt the file.
  - *Implementation strategy.* Prefer building the devcontainer config as a
    Python dict in `devcontainer.py` and emitting it with `json.dumps`. If the
    template is kept, register/apply `tojson` to every interpolated string and
    list field in `devcontainer.json.j2`.
  - *Test.* In `tests/test_devcontainer.py`, render with quotes/newlines in
    `project_name`, git identity, and an `extra_mounts` entry, then
    `json.loads` the output and assert structure is intact (no injected keys).

- **Do not mount a prompt file's whole parent directory.**
  - *Problem.* `cli.py:585` mounts `prompt_file.parent` read-only at
    `PROMPT_MOUNT_TARGET` (`/project-sandbox-prompt`, `cli.py:30`). A prompt from
    `$HOME` exposes the entire home directory to the agent.
  - *Implementation strategy.* In `cli.py`, copy the resolved prompt
    (`paths.resolve_strict`) into a private generated staging dir under
    `.project-sandbox/` and bind-mount only that file (or that single-purpose
    dir), keeping `PROJECT_SANDBOX_PROMPT_FILE` pointed at the staged path.
  - *Test.* Update the existing parent-mount expectation in `tests/test_cli.py`
    to assert only the staged prompt path is mounted, not its source parent.

- **Reject symlinked generated config/credential paths under `.project-sandbox`.**
  - *Problem (corrected).* The `/tmp/project-sandbox-*` credential **staging**
    dirs are already symlink-guarded by `_ensure_private_dir`
    (`config_agents.py:258-265`) and `_copy_path` (`:312-316`), and
    `test_staging_refuses_symlinked_credential_directory`
    (`tests/test_renderers.py:384`) covers that path. The genuinely outstanding
    gap is the **in-repo** `.project-sandbox/<agent>` paths: `render()`
    (`config_agents.py:71-89`) does `mkdir(..., exist_ok=True)` + `write_text`
    with no symlink check, and stale cleanup
    `_remove_stale_project_credentials` (`:268-271`) → `_remove_if_exists`
    (`:287`) calls bare `path.unlink()` on
    `.project-sandbox/claude/.credentials.json`. A hostile repo that pre-places
    `.project-sandbox/claude` as a symlink to `~/.claude` causes rendering to
    write outside the project and cleanup to delete the host credential file.
  - *Implementation strategy.* Before writing in `render()` and before
    unlinking in `_remove_stale_project_credentials` /
    `_remove_stale_project_agent_credentials`, `lstat` each managed component of
    the `.project-sandbox/<agent>` path and reject any symlink (mirroring
    `_ensure_private_dir`). Keep the existing `/tmp` guards as-is.
  - *Test.* In `tests/test_renderers.py`, pre-place `.project-sandbox/claude` as
    a symlink to an external dir and assert both `render()` and the stale-cleanup
    path raise rather than writing/deleting through the link.

- **Close firewall egress bypasses through DNS and broad ICMPv6.**
  - *Problem.* The firewall keeps DNS open to the resolver for arbitrary names
    (DNS-tunnel exfiltration), and `init-firewall.sh.j2:73-74` allows all
    outbound `ipv6-icmp` on INPUT and OUTPUT, which can carry data to arbitrary
    IPv6 hosts.
  - *Implementation strategy.* In `init-firewall.sh.j2`, route DNS through a
    constrained resolver/proxy or pre-resolve the allowed `DOMAINS` to IPs and
    block general DNS afterward; restrict ICMPv6 OUTPUT/INPUT to the
    neighbor-discovery / PMTU types and link-local scope actually required.
  - *Test.* Add a render assertion in `tests/test_renderers.py` for the narrowed
    ICMPv6 rule set and the constrained-DNS rule shape.

- **Fix the ignored-lockfile build contract.**
  - *Problem.* The root `Dockerfile` copies `uv.lock` and runs
    `uv sync --frozen`, and README (lines 68-86) documents the pattern, but
    `.gitignore` ignores `uv.lock`, so it is untracked and a clean checkout
    cannot build the documented image.
  - *Implementation strategy.* Commit `uv.lock` and remove the `uv.lock` entry
    from `.gitignore` (preferred, keeps `--frozen` reproducible). Alternative:
    drop the lockfile copy and `--frozen` from `Dockerfile` and the README.
  - *Test.* Update `tests/test_gitignore.py` to assert `uv.lock` is no longer in
    the ignore set (or that the Dockerfile no longer requires it).

### Medium

- **Terminate headless containers on parent interruption, not just timeout.**
  - *Problem.* `session.run()` (`session.py:51-58`) only tears down the process
    group on `subprocess.TimeoutExpired`. With `start_new_session=True`
    (`:44`), a `KeyboardInterrupt` or other parent exception leaves a named
    headless container running.
  - *Implementation strategy.* Add a `BaseException` handler around `proc.wait`
    that, when `proc.poll() is None`, calls `_terminate_process_group(proc,
    container_stop_argv=...)` and then re-raises. Keep the existing
    `TimeoutExpired` → `124` path and the `finally` stdout cleanup.
  - *Test.* In `tests/test_session.py`, make the mocked `proc.wait` raise
    `KeyboardInterrupt` and assert teardown ran and the exception propagated.

- **Validate fatal CLI inputs before creating a worktree.**
  - *Problem.* `_setup_worktree()` runs at `cli.py:150`, before build-source,
    prompt-path, and log-path validation. A typo'd `base_image`, bad
    `--dockerfile`, or missing `--prompt` can create a branch/worktree before
    failing.
  - *Implementation strategy.* Reorder `cli.py` so build source, prompt path,
    log-path parent, and other fatal inputs are validated before
    `_setup_worktree()`; alternatively track the newly created worktree and
    remove it on any pre-session failure.
  - *Test.* In `tests/test_cli.py`, pass an invalid build source / missing
    prompt and assert no worktree/branch is created before the error.

- **Run `gh pr create` from the target repo and surface failures.**
  - *Problem.* `worktree.teardown(..., after="pr")` invokes `gh pr create`
    without `cwd=repo` and ignores the exit status, so it can target the wrong
    repo or fail silently.
  - *Implementation strategy.* In `worktree.py`, run `gh pr create` with
    `cwd=str(repo)` and `check=True`; on failure, leave the worktree in place
    and print an actionable message.
  - *Test.* In `tests/test_worktree.py`, assert the `gh` invocation uses
    `cwd=str(repo)` and that a non-zero exit raises and preserves the worktree.

- **Narrow the devcontainer host-network firewall exception.**
  - *Problem.* `init-firewall.sh.j2:126-149` (the `allow_host_network` variant)
    allows the entire IPv4 interface CIDR `HOST_NET4`, not just the gateway,
    potentially exposing peers on the container subnet. The IPv6 path is already
    narrower.
  - *Implementation strategy.* Match the IPv6 behavior: allow only the default
    gateway address (and ideally only the specific attach/IDE ports) instead of
    the whole `HOST_NET4` CIDR.
  - *Test.* Render assertion in `tests/test_renderers.py` that the devcontainer
    variant allows the gateway, not the interface CIDR.

- **Pin or verify mutable remote installs in generated Dockerfiles.**
  - *Problem.* `Dockerfile.j2` installs root-level tooling via `curl | bash`
    (nodesource, `:46`), GitHub latest-release discovery for `jj` (`:50-67`),
    and unpinned `npm install -g ...@latest` (`:69-79`); the root `Dockerfile:2`
    uses `uv:latest`. A compromised upstream or bad latest release yields an
    image that later receives workspace and agent credentials.
  - *Implementation strategy.* Pin versions/digests, verify SHA256 checksums for
    downloaded artifacts, and make upgrades explicit, in `Dockerfile.j2` and the
    root `Dockerfile`.
  - *Test.* Render assertion in `tests/test_renderers.py` that generated install
    steps carry pinned versions/checksums (no `@latest`, no bare latest-release
    follow).

- **Handle `FROM --platform=...` when warning about non-apt base images.**
  - *Problem.* `dockerfile.py:82` (`r"\s*FROM\s+(\S+)"`) treats the first token
    after `FROM` as the image, so `FROM --platform=$BUILDPLATFORM alpine:3.19`
    parses the image as `--platform=...` and skips the Debian/Ubuntu warning in
    `_is_non_apt_image` (`:88-90`).
  - *Implementation strategy.* Parse and skip `FROM` options (`--platform=...`
    and any other `--flag`/`--flag value`) before extracting the image token.
  - *Test.* In `tests/test_renderers.py`, assert a `FROM --platform=... alpine`
    line still triggers the non-apt warning.

- **Do not silently drop mixed Dockerfile `RUN` blocks.**
  - *Problem.* `_remove_restricted_user_setup()` (`dockerfile.py:140-148`) drops
    an entire `RUN` block when `_is_restricted_user_setup` (`:176-190`) matches
    `useradd`/`groupadd`/etc., so a mixed
    `apt-get install ... && useradd app && make setup` loses unrelated work.
  - *Implementation strategy.* Detect mixed user-management RUN blocks and
    reject them with an actionable error, or rewrite only the restricted
    subcommand and keep the rest of the block.
  - *Test.* In `tests/test_renderers.py`, feed a mixed RUN block and assert the
    chosen behavior (error or surgical removal preserving the other commands).

- **Align timeout documentation with the actual stop behavior.**
  - *Problem (corrected).* The mismatch is localized to `build_stop_argv`
    (`container_cli.py:151-157`), which uses runtime `kill` (immediate SIGKILL,
    no grace). The `_terminate_process_group` docstring
    (`session.py:69-74`), README (line 184), and the existing unit tests
    (`tests/test_session.py:94-115`, which mock `stop --time 5`) all describe a
    graceful `stop`.
  - *Implementation strategy.* Switch `build_stop_argv` to a bounded `stop`
    (e.g. `stop --time <grace>`) so the runtime sends SIGTERM then force-kills,
    aligning the code with README, the session docstring, and the tests.
  - *Test.* Add a direct `tests/test_container_cli.py` test for
    `build_stop_argv` (none exists today) asserting bounded `stop` argv.

- **Make the e2e smoke test cover all documented devcontainer artifacts.**
  - *Problem.* `scripts/e2e-test.sh` omits `.project-sandbox/Dockerfile.devcontainer`,
    `init-firewall-devcontainer.sh`, `claude-devcontainer/settings.json`, and
    `codex-devcontainer/config.toml`; its symlink check validates the `readlink`
    target prefix but not that the target file exists, so dangling links pass.
  - *Implementation strategy.* Add the four artifacts to `REQUIRED` and verify
    each symlink target resolves to an existing file (`-e` on the resolved
    target), in `scripts/e2e-test.sh`.
  - *Test.* This is the test artifact itself; verify by running
    `scripts/e2e-test.sh` against the generated project.

- **Use safe Markdown fences in generated transcripts.**
  - *Problem.* `transcript.py:106-135` wraps tool input/output in fixed
    triple-backtick fences; tool output containing ``` breaks out and injects
    Markdown/HTML into the sidecar transcript.
  - *Implementation strategy.* Compute a fence longer than the longest backtick
    run in the content (or indent/escape the body) before wrapping in
    `_render_tool_use` / `_render_user`.
  - *Test.* In `tests/test_transcript.py`, render tool output containing a
    ```` ``` ```` run and assert it does not break the fence.

### Low

- **Avoid default session log collisions.**
  - *Problem.* `default_log_path` (`session.py:10-16`) uses second-resolution
    timestamps (`%Y%m%d-%H%M%S`) and `run()` opens the file `"w"` (`:35`), so two
    same-agent sessions started in one second overwrite each other.
  - *Implementation strategy.* Add higher-resolution time or a short UUID to the
    stem, or use exclusive-create with retry, in `default_log_path`.
  - *Test.* In `tests/test_session.py`, request two paths within the same second
    and assert they differ.

- **Make devcontainer history initialization robust to apostrophes in paths.**
  - *Problem.* `devcontainer.py:86-88` builds `initializeCommand` as a
    single-quoted shell string (`mkdir -p '<path>/shell' ...`); a workspace path
    containing `'` creates wrong directories and breaks later bind mounts.
  - *Implementation strategy.* Emit `initializeCommand` in array form (no shell
    quoting) so the path is passed as a literal argument.
  - *Test.* In `tests/test_devcontainer.py`, render with a workspace path
    containing `'` and assert the command is array-form / not broken.

- **Warn on `npm ci` in non-`/workspace` Dockerfiles.**
  - *Problem.* `_LOCAL_INSTALL_RE` (`dockerfile.py:21`) matches `npm install`
    but not `npm ci`, so Node projects using `npm ci` miss the WORKDIR-mismatch
    warning.
  - *Implementation strategy.* Extend the regex to also match `npm ci`.
  - *Test.* In `tests/test_renderers.py`, add `npm ci` to the warning coverage
    alongside the existing `npm install` case.

- **Make the timeout teardown verifier target only its own container.**
  - *Problem.* `scripts/verify-timeout-teardown.sh:69-76` diffs all running
    container IDs (`ls -q`/`ps -q`) before and after, so unrelated containers
    started during the settle window look like leaks.
  - *Implementation strategy.* Capture/expose the generated project-sandbox
    container name (or a known label) and filter the before/after listing by it.
  - *Test.* This is the verification script itself; confirm by running it with
    an unrelated container present and seeing no false leak.
